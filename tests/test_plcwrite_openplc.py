"""`naga plc-write` against a real soft PLC (Phase 15 FE): the OpenPLC v3 runtime in Docker, on
this machine only. Skipped unless `docker` is on PATH and NAGA_OPENPLC_SMOKE=1; run on request:

    docker build -t openplc:v3 https://github.com/thiagoralves/OpenPLC_v3.git
    NAGA_OPENPLC_SMOKE=1 pytest -q -m live tests/test_plcwrite_openplc.py

The image is the person's own build of the OpenPLC repository (NAGA_OPENPLC_IMAGE names another
tag). The test starts the container with its Modbus and web ports published on 127.0.0.1 only,
loads a one-variable program through the runtime's web pages (default login openplc/openplc),
runs `plc-write` dry-run then `--write` for one approved setpoint, checks the value read back
and the audit, and removes the container whatever happens.
"""
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar

import pytest

from test_plcwrite import audit, files, plc_write, proposal, queued

pytestmark = [pytest.mark.live, pytest.mark.slow,
              pytest.mark.skipif(shutil.which("docker") is None
                                 or os.environ.get("NAGA_OPENPLC_SMOKE") != "1",
                                 reason="needs docker and NAGA_OPENPLC_SMOKE=1")]

IMAGE = os.environ.get("NAGA_OPENPLC_IMAGE", "openplc:v3")
# %QW0 is holding register 0; it starts at 30 (3.0 at the setpoints file's scale of 0.1) and
# the program never assigns it, so what Modbus writes is what is read back. OpenPLC's compiler
# (matiec) wants located and plain variables in separate VAR blocks.
PROGRAM = """PROGRAM smoke
  VAR
    cycle AT %QW0 : INT := 30;
  END_VAR
  VAR
    tick : INT;
  END_VAR
  tick := tick + 1;
END_PROGRAM

CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK task0(INTERVAL := T#100ms, PRIORITY := 0);
    PROGRAM instance0 WITH task0 : smoke;
  END_RESOURCE
END_CONFIGURATION
"""


def docker(*args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          check=True).stdout.strip()


def published(name, port):
    """The host port Docker chose for the container's *port* (bound to 127.0.0.1)."""
    said = docker("port", name, f"{port}/tcp").splitlines()[0]
    host, _, number = said.rpartition(":")
    assert host == "127.0.0.1", said        # loopback only, as the writer requires (D744)
    return int(number)


def until(test, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if test():
                return
        except OSError:
            pass
        time.sleep(1.0)
    pytest.fail(f"OpenPLC: {what} within {seconds} s")


def multipart(fields, file_field, filename, content):
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                 f'filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n{content}\r\n')
    return f"{''.join(parts)}--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def load_program(web):
    """Log in, upload PROGRAM, compile it and start the PLC, through the runtime's own pages."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    base = f"http://127.0.0.1:{web}"

    def call(path, data=None, kind=None):
        request = urllib.request.Request(base + path, data=data)
        if kind:
            request.add_header("Content-Type", kind)
        with opener.open(request, timeout=30) as answer:
            return answer.read().decode("utf-8", "replace")

    call("/login", urllib.parse.urlencode({"username": "openplc", "password": "openplc"}).encode(),
         "application/x-www-form-urlencoded")
    body, kind = multipart({}, "file", "smoke.st", PROGRAM)
    page = call("/upload-program", body, kind)
    # The upload page names the stored file in a hidden field (value before name in the
    # runtime's markup); the action form records it.
    found = re.search(r"value='([^']*)' id='prog_file' name='prog_file'", page)
    assert found, page[:500]
    stored = found.group(1)
    call("/upload-program-action", urllib.parse.urlencode({
        "prog_name": "naga smoke", "prog_descr": "plc-write smoke test", "prog_file": stored,
        "epoch_time": str(int(time.time()))}).encode(), "application/x-www-form-urlencoded")
    call(f"/compile-program?file={urllib.parse.quote(stored)}")
    def compiled():
        logs = call("/compilation-logs")
        assert "finished with errors" not in logs, logs[-1500:]
        return "Compilation finished successfully" in logs

    until(compiled, 300, "the program compiled")
    call("/start_plc")


def test_plc_write_writes_one_approved_setpoint_to_openplc_and_reads_it_back(tmp_path, plant_net):
    name = f"naga-openplc-{uuid.uuid4().hex[:8]}"
    try:
        docker("run", "-d", "--name", name, "-p", "127.0.0.1::502", "-p", "127.0.0.1::8080", IMAGE)
        modbus, web = published(name, 502), published(name, 8080)
        until(lambda: urllib.request.urlopen(f"http://127.0.0.1:{web}/login", timeout=5).status
              == 200, 120, "the web pages answered")
        load_program(web)
        until(lambda: socket.create_connection(("127.0.0.1", modbus), timeout=2).close() or True,
              60, "Modbus answered")
        connections, setpoints = files(tmp_path, modbus)
        record = proposal(2.0)
        queued(tmp_path, record)                # a person's "taken" reply, by Matteo
        code, said = plc_write(tmp_path, connections, setpoints)
        assert code == 0 and said[-1].endswith("would be written (dry-run; the PLC holds 3)"), said
        code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
        assert code == 0 and said[-1].endswith("written (was 3, read back 2)"), said
        lines = audit(tmp_path)
        assert [(e["mode"], e["phase"]) for e in lines] == [
            ("dry-run", "before"), ("dry-run", "after"), ("write", "before"), ("write", "after")]
        assert lines[-1]["outcome"] == "written" and lines[-1]["id"] == record["id"]
        # Written once: a second run finds nothing left to write.
        code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
        assert code == 0 and said[-1] == "no setpoint of the outbox is left to write", said
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
