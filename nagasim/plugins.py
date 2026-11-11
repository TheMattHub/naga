"""Plugin loading for Naga blocks, distributions and exporters (ROADMAP 2.4 B).

SECURITY: a plugin is arbitrary Python code, imported and executed with the
same trust level as `nagasim/core/custom_logic.py` (ARCHITECTURE 7.4) -- only
point NAGASIM_PLUGIN_PATH at, or `pip install`, code you trust.

Two ways to add third-party blocks/distributions/exporters, both optional and
both completely inert until `load_plugins()` runs (called once from
`launcher.main` and, for library use with no launcher, lazily on
`nagasim.api.run_model`'s first call):

1. Installed packages exposing entry points in three groups, each entry
   resolving to one spec object:
   - "nagasim.blocks"        -> a `nagasim.core.block_spec.BlockSpec`
                                (optionally with its `ui` field set to a
                                `BlockUi` below, for the palette/canvas)
   - "nagasim.distributions" -> a `nagasim.core.distribution_spec.DistributionSpec`
   - "nagasim.exporters"     -> a `nagasim.exporters.Exporter`
2. A no-install developer path: the NAGASIM_PLUGIN_PATH environment variable
   (os.pathsep-separated directories). Every `*.py` file directly inside one
   is imported and may expose module-level `NAGASIM_BLOCKS` /
   `NAGASIM_DISTRIBUTIONS` / `NAGASIM_EXPORTERS` lists of the same three spec
   types. `examples/plugins/inspection_plugin/` (in the repository, not in
   the wheel) works both ways; see its README.

3. The plugin folders a person trusted in Settings > Plugins
   (`settings/trusted.py`, kind "plugin_folder", enabled), scanned the same way
   (D1115), each only while its files match the digest stored at the person's
   tick (D1194): a changed folder loads nothing, logs one line and is listed in
   the report's `changed`. `load_plugin_folder` imports one more such folder in a running
   process, right after the person trusts it. A plugin that imports its own
   project's packages puts that project's root on `sys.path` itself, at the top
   of its file: the loader adds no import roots (D1116).

In a frozen build (`sys.frozen`), `<user data dir>/plugins` is scanned the same
way as a NAGASIM_PLUGIN_PATH directory, so a user can drop a plugin file in
without a console. That directory is `settings/userpaths.user_data_dir()`:
`%APPDATA%/the product` on Windows, `$XDG_DATA_HOME/the product` elsewhere, and
`NAGASIM_USER_DIR` overrides it on every OS.

A plugin that raises on import, or whose spec name collides with a built-in
(or another plugin already loaded), is skipped and recorded in
`PluginReport.failed` -- never fatal to the rest of the application.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nagasim.core.block_registry import BLOCK_REGISTRY, PLUGIN_BLOCK_TYPES
from nagasim.core.distributions import DISTRIBUTION_REGISTRY, _rebuild_derived_tables
from nagasim.exporters import EXPORTER_REGISTRY
from nagasim.settings import trusted
from nagasim.settings.userpaths import user_data_dir

log = logging.getLogger(__name__)

__all__ = ["BlockUi", "PluginReport", "load_plugins", "load_plugin_folder", "reset_plugins",
           "search_paths"]

_ENV_VAR = "NAGASIM_PLUGIN_PATH"
# Set by `naga --no-plugins` (S8-15): this process and its workers load no plugin at all.
NO_PLUGINS_ENV = "NAGASIM_NO_PLUGINS"
# entry-point group -> (registry dict, attribute holding the spec's own key)
_GROUPS = {
    "nagasim.blocks": (BLOCK_REGISTRY, "type_name"),
    "nagasim.distributions": (DISTRIBUTION_REGISTRY, "name"),
    "nagasim.exporters": (EXPORTER_REGISTRY, "name"),
}
# the matching NAGASIM_* dev-path list name for each group, in the same order
_DEV_LISTS = {
    "nagasim.blocks": "NAGASIM_BLOCKS",
    "nagasim.distributions": "NAGASIM_DISTRIBUTIONS",
    "nagasim.exporters": "NAGASIM_EXPORTERS",
}


@dataclass(frozen=True)
class BlockUi:
    """Optional palette/canvas metadata for a plugin block (ROADMAP 2.4 D).
    Attach it to a BlockSpec's `ui` field; a block with none draws as a
    neutral rectangle (ui/style.py's existing default) under the "Plugins"
    palette group."""
    color: str                     # a Qt colour name or "#RRGGBB"
    shape: str = "rect"             # "rect" | "pill" | "diamond" | "double"
    icon_path: str | None = None    # optional image file for the palette icon
    category: str = "Plugins"       # palette grouping


@dataclass
class PluginReport:
    """Result of one `load_plugins()` call. `loaded` entries read
    "<kind>:<name>" (e.g. "blocks:INSPECT"); `failed` entries are
    (source, error message) pairs, *source* being the file path or entry
    point that failed."""
    loaded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    # {folder as scanned: the block types its files registered}: what Settings > Plugins
    # lists under each folder. A folder scanned with nothing registered maps to [].
    folders: dict[str, list[str]] = field(default_factory=dict)
    # trusted folders skipped because their files moved since the person's tick (D1194, #1738)
    changed: list[str] = field(default_factory=list)

    def has_folder(self, directory) -> bool:
        key = _key(directory)
        return any(_key(d) == key for d in self.folders)


_report: PluginReport | None = None


def reset_plugins() -> None:
    """Test hook: forget that plugins were ever loaded, so the next
    `load_plugins()` call runs again. Does NOT unregister anything already
    added to BLOCK_REGISTRY/DISTRIBUTION_REGISTRY/EXPORTER_REGISTRY -- tests
    that need real isolation run `load_plugins()` in a subprocess instead
    (see tests/test_plugins.py)."""
    global _report
    _report = None


def load_plugins(force: bool = False) -> PluginReport:
    """Discover and register plugin blocks/distributions/exporters.

    Runs once per process; a second call returns the same report without
    doing anything, unless *force* is True (tests only -- see reset_plugins).
    """
    global _report
    if _report is not None and not force:
        return _report

    report = PluginReport()
    if os.environ.get(NO_PLUGINS_ENV) == "1":
        _report = report
        return report
    for directory in search_paths():
        if not report.has_folder(directory):    # the same folder named twice is read once
            _load_dev_path(directory, report, _pinned(directory))
    _load_entry_points(report)
    _rebuild_derived_tables()
    _report = report
    if report.failed:
        log.warning("plugins: %d loaded, %d failed", len(report.loaded), len(report.failed))
    return report


def load_plugin_folder(directory) -> PluginReport:
    """Import one more plugin folder now (a folder the person has just trusted) and
    return the process's report with it added. A folder already scanned is not imported
    again. Registration cannot be undone: disabling a folder takes effect at the next
    start."""
    report = load_plugins()
    directory = Path(directory)
    if directory.is_dir() and not report.has_folder(directory):
        _load_dev_path(directory, report, _pinned(directory))
        _rebuild_derived_tables()
    return report


def _key(directory) -> str:
    return os.path.normcase(str(Path(directory).resolve()))


def search_paths() -> list[Path]:
    """The plugin folders `load_plugins` scans, in order: NAGASIM_PLUGIN_PATH, the
    enabled trusted folders, then a frozen build's own drop folder."""
    env = os.environ.get(_ENV_VAR, "")
    paths = [Path(p) for p in env.split(os.pathsep) if p]
    paths += trusted.enabled_paths("plugin_folder")
    if getattr(sys, "frozen", False):
        # DEVLOG #107: resolved off %APPDATA% alone, so a frozen build on
        # Linux/macOS had no user plugin folder at all.
        paths.append(user_data_dir() / "plugins")
    return [p for p in paths if p.is_dir()]


def _pinned(directory: Path):
    """The trusted entry whose digest *directory* must match, or None for a folder the caller's own
    environment names (NAGASIM_PLUGIN_PATH, a frozen build's drop folder): those are not pinned."""
    key = _key(directory)
    if any(_key(p) == key for p in os.environ.get(_ENV_VAR, "").split(os.pathsep) if p):
        return None
    entry = trusted.find("plugin_folder", directory)
    return entry if entry is not None and entry.enabled else None


def _load_dev_path(directory: Path, report: PluginReport, pinned=None) -> None:
    try:
        files = trusted.folder_files(directory)
    except OSError as exc:
        report.failed.append((str(directory), str(exc)))
        log.warning("plugin folder %s does not read: %s", directory, exc)
        return
    if pinned is not None:
        if trusted.changed(pinned, files):
            report.changed.append(str(directory))
            log.warning("plugin folder %s changed since it was trusted: nothing loaded from it until it is "
                        "enabled again in Settings > Plugins", directory)
            return
        log.info("plugins: importing trusted folder %s", directory)
    provided = report.folders.setdefault(str(directory), [])
    if str(directory) in report.changed:
        report.changed.remove(str(directory))
    for py_file, data in files:
        module_name = f"_nagasim_plugin_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file,
                                                          loader=trusted.BytesLoader(py_file, data))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            report.failed.append((str(py_file), str(exc)))
            log.warning("plugin %s failed to import: %s", py_file, exc)
            continue
        for group, list_name in _DEV_LISTS.items():
            added = _register_many(getattr(module, list_name, []), group, str(py_file), report)
            if group == "nagasim.blocks":
                provided.extend(added)


def _load_entry_points(report: PluginReport) -> None:
    try:
        eps = importlib.metadata.entry_points()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not read plugin entry points: %s", exc)
        return
    for group in _GROUPS:
        try:
            group_eps = eps.select(group=group)   # Python >= 3.10 API
        except AttributeError:
            group_eps = eps.get(group, [])         # pragma: no cover - older API
        for ep in group_eps:
            try:
                obj = ep.load()
            except Exception as exc:
                report.failed.append((ep.value, str(exc)))
                log.warning("plugin entry point %s failed to load: %s", ep.value, exc)
                continue
            _register_many([obj], group, ep.value, report)


def _register_many(objs, group: str, source: str, report: PluginReport) -> list[str]:
    """Register *objs*; returns the keys that were registered."""
    registry, key_attr = _GROUPS[group]
    kind = group.rsplit(".", 1)[-1]
    added = []
    for obj in objs:
        key = getattr(obj, key_attr, None)
        if not key:
            report.failed.append((source, f"{kind} spec has no usable '{key_attr}'"))
            continue
        if key in registry:
            report.failed.append((source, f"'{key}' redefines a built-in or already-loaded {kind[:-1]}"))
            log.warning("plugin %s rejected: '%s' redefines a built-in or already-loaded %s",
                       source, key, kind[:-1])
            continue
        registry[key] = obj
        report.loaded.append(f"{kind}:{key}")
        added.append(key)
        if group == "nagasim.blocks":
            PLUGIN_BLOCK_TYPES.add(key)
    return added
