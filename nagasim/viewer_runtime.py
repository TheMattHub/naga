"""The browser runtime of the viewer (D321, D322): which Pyodide, where a copy is kept,
how it is checked and the one place that downloads it. A security review of the viewer's
network access reads this file alone.

    fetch(directory)       download the pinned files; only `naga viewer --fetch` and the
                           editor's consent dialog call it
    load(directory)        {file name: bytes} of a copy, every byte checked

* ONE constant pins everything: the SHA-256 of Pyodide's core archive. The wheels are
  checked against the SHA-256 the lock file INSIDE that archive states for them.
* Every hop of a download is https (`_HttpsOnly`: a redirect to anything else is
  refused); integrity does not rest on that but on the pinned hashes.
* The two addresses are written here. No URL, host or hash comes from a model file, a
  setting or an agent tool, and nothing here runs unless the caller asked (rule 10).
* A copy is the archive and the wheels as downloaded, in one folder. `load` verifies it
  on every use, so a folder carried to a machine with no network (`--runtime DIR`) is
  held to the same pin.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import urllib.request
from pathlib import Path

from nagasim.settings.userpaths import user_data_dir

__all__ = ["VERSION", "ENV_VAR", "CORE_FILES", "PACKAGES", "ViewerRuntimeError", "Cancelled",
           "default_dir", "resolve_dir", "wheels", "load", "fetch"]

VERSION = "314.0.7"
CORE_NAME = f"pyodide-core-{VERSION}.tar.bz2"
CORE_URL = f"https://github.com/pyodide/pyodide/releases/download/{VERSION}/{CORE_NAME}"
CORE_SHA256 = "2abdcc2e35208af406e07724cffa85bc582ced97e9028383ecf5462541393f95"
WHEEL_URL = f"https://cdn.jsdelivr.net/pyodide/v{VERSION}/full/"
# What the page starts Python from, in the archive's `pyodide/` folder.
CORE_FILES = ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip",
              "pyodide-lock.json")
# What the engine imports; their dependencies are the lock file's. tzdata: pandas asks
# for it where the system has no time zones, and a browser has none.
PACKAGES = ("numpy", "pandas", "scipy", "tzdata")
ENV_VAR = "NAGASIM_VIEWER_RUNTIME"
# Ceilings on what is read or downloaded: the archive is 6.8 MB, scipy's wheel 14 MB.
_MAX_ARCHIVE, _MAX_FILE, _MAX_WHEELS = 16_000_000, 32_000_000, 24


class ViewerRuntimeError(ValueError):
    """The runtime is missing or is not the pinned one. A ValueError, so the command
    line prints it as one line."""


class Cancelled(Exception):
    """The caller's `should_stop` said to stop: nothing was written."""


def default_dir() -> Path:
    return user_data_dir() / "viewer-runtime" / f"pyodide-{VERSION}"


def resolve_dir(directory=None) -> Path:
    """*directory*, else `NAGASIM_VIEWER_RUNTIME`, else the user's own copy."""
    return Path(directory or os.environ.get(ENV_VAR) or default_dir())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _core(archive: bytes) -> dict[str, bytes]:
    """The core files out of the archive's bytes, which must be the pinned ones."""
    if _sha256(archive) != CORE_SHA256:
        raise ViewerRuntimeError(f"{CORE_NAME} is not the pinned file (its SHA-256 differs)")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:bz2") as tar:
        for name in CORE_FILES:
            member = tar.getmember(f"pyodide/{name}")
            if not member.isfile() or member.size > _MAX_FILE:
                raise ViewerRuntimeError(f"{CORE_NAME}: {name} is not a file of a size expected")
            files[name] = tar.extractfile(member).read()
    return files


def wheels(lock: bytes) -> list[tuple[str, str]]:
    """[(file name, sha256)] of `PACKAGES` and what they depend on, from the lock file,
    in the order the names sort."""
    packages = json.loads(lock)["packages"]
    found: dict[str, tuple[str, str]] = {}
    todo = list(PACKAGES)
    while todo:
        name = todo.pop()
        if name in found:
            continue
        entry = packages[name]
        file_name = str(entry["file_name"])
        if "/" in file_name or "\\" in file_name or not file_name.endswith(".whl"):
            raise ViewerRuntimeError(f"the lock file names '{file_name[:80]}' for {name}: not a wheel")
        found[name] = (file_name, str(entry["sha256"]))
        todo.extend(entry.get("depends") or [])
    if len(found) > _MAX_WHEELS:
        raise ViewerRuntimeError("the lock file asks for more wheels than a viewer holds")
    return [found[name] for name in sorted(found)]


def _missing(directory: Path, what: str) -> ViewerRuntimeError:
    return ViewerRuntimeError(
        f"the browser runtime (Pyodide {VERSION}) is not complete in {directory}: {what}. "
        f"Run `naga viewer --fetch` once on a machine with network access (29 MB from "
        f"github.com and cdn.jsdelivr.net), or pass --runtime DIR (or set {ENV_VAR}) "
        f"naming a folder copied from such a machine")


def load(directory=None) -> dict[str, bytes]:
    """{file name: bytes}: the core files, then the wheels, each checked against the pin.
    Raises `ViewerRuntimeError` naming the remedy when a file is absent or is another file."""
    directory = resolve_dir(directory)
    archive = directory / CORE_NAME
    if not archive.is_file():
        raise _missing(directory, f"{CORE_NAME} is not there")
    if archive.stat().st_size > _MAX_ARCHIVE:
        raise ViewerRuntimeError(f"{archive} is larger than the pinned archive")
    files = _core(archive.read_bytes())
    for file_name, digest in wheels(files["pyodide-lock.json"]):
        path = directory / file_name
        if not path.is_file():
            raise _missing(directory, f"{file_name} is not there")
        if path.stat().st_size > _MAX_FILE:
            raise ViewerRuntimeError(f"{path} is larger than a wheel of the runtime")
        data = path.read_bytes()
        if _sha256(data) != digest:
            raise ViewerRuntimeError(f"{path} is not the pinned file (its SHA-256 differs)")
        files[file_name] = data
    return files


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """GitHub answers a release download with a redirect to its storage host. Any hop is
    followed only to another https address: never down to http, never to a file."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).lower().startswith("https://"):
            raise ViewerRuntimeError(f"{req.full_url} redirects to an address that is not "
                                     "https: refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float):
    """`urlopen` with `_HttpsOnly`. The environment's proxies stay in force
    (`build_opener` keeps the default handlers): they are the caller's own."""
    if not url.startswith("https://"):
        raise ViewerRuntimeError(f"{url} is not an https address")
    return urllib.request.build_opener(_HttpsOnly()).open(url, timeout=timeout)


def _download(url: str, limit: int, opener) -> bytes:
    with opener(url, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ViewerRuntimeError(f"{url} sent more than {limit} bytes")
    return data


def _keep(directory: Path, name: str, data: bytes) -> None:
    """Written whole or not at all: a temporary name, then a rename."""
    temporary = directory / (name + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, directory / name)


def fetch(directory=None, *, opener=_open, say=lambda text: None, should_stop=None) -> Path:
    """Download what `load` needs and *directory* does not hold yet; return the folder.
    A file is kept only when it is the pinned one. *opener* is `_open` (https at every
    hop, the environment's proxies; the tests give their own); *say* is told each file as it arrives; *should_stop* is asked before
    each file, and a yes raises `Cancelled` (the files already kept stay: each is whole
    and pinned)."""
    directory = resolve_dir(directory)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / CORE_NAME
    if not (archive.is_file() and _sha256(archive.read_bytes()) == CORE_SHA256):
        say(f"{CORE_URL}")
        data = _download(CORE_URL, _MAX_ARCHIVE, opener)
        _core(data)                                 # refuses another file before it is kept
        _keep(directory, CORE_NAME, data)
    lock = _core(archive.read_bytes())["pyodide-lock.json"]
    for file_name, digest in wheels(lock):
        path = directory / file_name
        if path.is_file() and _sha256(path.read_bytes()) == digest:
            continue
        if should_stop is not None and should_stop():
            raise Cancelled("the download was cancelled")
        say(WHEEL_URL + file_name)
        data = _download(WHEEL_URL + file_name, _MAX_FILE, opener)
        if _sha256(data) != digest:
            raise ViewerRuntimeError(f"{WHEEL_URL}{file_name} is not the file the lock file pins")
        _keep(directory, file_name, data)
    return directory
