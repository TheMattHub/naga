"""The live control room of a study in a browser (`naga room`, `naga mcp --room`).

A stdlib HTTP server on 127.0.0.1 that serves the `studyapp` shell over fetch, the rendered page, the
study's own files read-only, a change signal the page polls, and the direct buttons (a short list of
study tools). It is a new I/O surface reachable from any web page the person has open, so every
request must prove it is the person's own page:

* loopback only, a random port, and a random token: in the URL on the first load, then in an
  HttpOnly SameSite=Strict cookie (a foreign site cannot read or send it);
* the Host header must be the loopback address and our port (DNS rebinding);
* a POST needs a JSON body and an Origin equal to our own (CSRF); bodies are capped;
* files are confined to the study folder (no `..`, no drive, no symlink out);
* no log line carries the query string (the token).

Permission is the caller's: the room is started by the person (`naga room`, or the `--room` flag of
`naga mcp`), never by a tool argument, and its buttons reach only the tools in `DIRECT`.

The room's assistant (`StudyAdapter` under `assistant.AssistantSession`) is offered the study tools
plus `explain_kpi` and `what_if` (on the study's last version, never saved), always on the room's own
study, with a model's Python refused. Its network is the hosting
process's workspace policy (what the person granted, none by default), as in `naga mcp --room`. The provider and its key come
from the person's configuration (`llm.resolve_config`, as for `naga assist`) and stay in this process:
every error is redacted with the key before it becomes an event, and the conversation lives in memory.

What next (D727, D728): a "What next?" turn first shows the study's rule options (an "options" event);
with an allow-listed hosted model (`nextideas.enabled`) the model's own checked ideas follow once the
turn has ended (an "ideas" event). Neither runs anything: the person picks, and the pick is a message.
A turn sent by voice is marked so in the text the model reads (`assistant.VOICE_NOTE`)."""
from __future__ import annotations

import argparse
import http.server
import json
import logging
import mimetypes
import re
import secrets
import sys
import threading
import webbrowser
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ACTIVE = None   # the running Room, set by Room.start

MAX_BODY = 1_000_000
# The direct buttons: tools that make a file or take one recorded step the person asked for.
# study_update is allowed only as {"plan_approved": true} (checked in _tool).
DIRECT = frozenset({"study_report", "study_view", "study_package", "study_sensitivity", "study_extensions",
                    "study_animation", "study_writeup", "study_slides", "study_revert", "study_accept"})
_LOG = logging.getLogger(__name__)
_SHELL_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
              "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def url_for(study_id: str) -> str | None:
    """The room address of a study while a room runs, else None."""
    return None if ACTIVE is None else ACTIVE.url(study_id)


NO_MODEL = ("No assistant model is set. Set ANTHROPIC_API_KEY, or a local model as for naga assist, "
            "then restart the room.")
# What a tool call looks like to the person in the assistant panel: its purpose, never its arguments.
_PURPOSE = {
    "study_start": "Starting a study", "study_status": "Reading where the study stands",
    "explain_kpi": "Explaining a KPI", "what_if": "Testing a change",
    "study_update": "Recording what was learned", "study_add_input": "Storing an input",
    "study_step": "Running the next version", "study_validate": "Checking against a textbook answer",
    "study_accept": "Recording the acceptance", "study_revert": "Going back to an earlier version",
    "study_report": "Writing a report", "study_package": "Building the package",
    "study_view": "Drawing the flowchart", "study_show": "Showing the control room",
    "study_sensitivity": "Testing which assumptions change the answer",
    "study_extensions": "Looking for extensions", "study_animation": "Making an animation",
    "study_writeup": "Writing the write-up", "study_slides": "Making the slides",
}


def provider_from_env():
    """(provider, key) from the person's configuration, or (None, None) when none is set: the room
    then serves the direct buttons and its assistant panel says how to set a model."""
    from nagasim import llm
    try:
        cfg = llm.resolve_config()
        return llm.build_provider(cfg), cfg.api_key
    except (llm.LLMError, ImportError):
        return None, None


# C14: the room may also ask what makes up a KPI and what a change would do, on the study's last
# version; neither edits a model or records anything (a recorded change is a study_step).
MODEL_TOOLS = ("explain_kpi", "what_if")
# A message that is only "what next?" gets the same as the What next? button.
_WHAT_NEXT = re.compile(r"^\s*(what('s| is)? next|what now|what else)\s*\??\s*$", re.IGNORECASE)


class StudyAdapter:
    """`WorkspaceAdapter`'s methods over one study: the study tools (`study_page_html` is the page's
    own) plus `MODEL_TOOLS`, the study argument forced to this study, every call under the shared tool
    lock with a model's Python refused. A `MODEL_TOOLS` call runs on the study's last version, loaded
    into the study's own workspace slot, whatever model it names. It uses the process's workspace (the
    room's root) and never installs one: in `naga mcp --room` that workspace is the MCP server's. A
    turn is not one undo step: study versions and study_revert are the undo, so the turn methods do
    nothing."""

    def __init__(self, root: Path, study_id: str, lock: threading.Lock, on_tool=None):
        from nagasim import agent_tools
        self.root, self.study_id, self.lock, self.on_tool = Path(root), study_id, lock, on_tool
        self.names = self.tool_names()
        self.offered = frozenset()

    @staticmethod
    def tool_names() -> list[str]:
        from nagasim import agent_tools
        return sorted([n for n in agent_tools.TOOLS if n.startswith("study_") and n != "study_page_html"]
                      + list(MODEL_TOOLS))

    def offer(self, names) -> None:
        self.offered = frozenset(names)

    def begin_turn(self) -> None:
        pass

    def commit_turn(self) -> None:
        pass

    def rollback_turn(self, exc=None) -> None:
        pass

    def run_tool(self, name: str, arguments: dict | None) -> dict:
        from nagasim import agent_tools
        if name not in self.offered:
            return agent_tools.ToolError("E_TOOL_NOT_OFFERED", f"'{name}' is not a tool of this session",
                                         f"use one of: {', '.join(sorted(self.offered))}").as_dict()
        if arguments is None:
            return agent_tools.ToolError("E_BAD_ARGUMENTS", f"the arguments of '{name}' were not a JSON object",
                                         "send the arguments as one JSON object").as_dict()
        if self.on_tool is not None:
            self.on_tool(name)
        out = self._call(name, arguments)
        if self.on_tool is not None and (failed := _failure(out)):
            self.on_tool(name, failed)
        return out

    def _version_slot(self) -> str:
        """Load the study's last version into its workspace slot and name the slot; ToolError when
        the study has no version yet. The folder is confined to the workspace and the version id
        checked (`study.version_id`), as every study tool's are."""
        from nagasim.agent_tools import workspace
        from nagasim.agent_tools.study import _load_slot, _open, _slot
        folder, _s = _open(self.study_id)
        slot = _slot(folder)
        workspace().models[slot] = _load_slot(workspace(), slot)
        return slot

    def _call(self, name: str, arguments: dict) -> dict:
        from nagasim import agent_tools
        with self.lock, agent_tools.python_consent_dropped():
            if name in MODEL_TOOLS:
                try:
                    model = self._version_slot()
                except agent_tools.ToolError as exc:
                    return exc.as_dict()
                rest = {k: v for k, v in arguments.items() if k not in ("study", "model")}
                out = agent_tools.call(name, model=model, **rest)
            else:
                out = agent_tools.call(name, **{**arguments, "study": self.study_id})
        # The person is in the room already: its link (token included) is not the assistant's to paste.
        return {k: v for k, v in out.items() if k not in ("room_url", "page_url")} if isinstance(out, dict) else out

    def model_summary(self) -> dict:
        return self._call("study_status", {})


def _failure(out) -> str | None:
    """'refused (E_CODE)' for a refused call, 'draft (E_CODE)' for a step stored as a draft, else None."""
    if not isinstance(out, dict):
        return None
    if isinstance(out.get("error"), dict):
        return f"refused ({out['error'].get('code', 'error')})"
    if out.get("kind") == "draft":
        from nagasim import study as study_mod
        return f"draft ({study_mod.error_code(out.get('problems'))})"
    return None


class _Chat:
    """One study's conversation with the room's assistant: events in memory, one turn at a time."""

    def __init__(self):
        self.events: list[dict] = []
        self.running = False
        self.pending = 0            # ideas still being asked for (layer 2): the page keeps polling
        self.cancel = threading.Event()
        self.session = None
        self.guard = threading.Lock()

    def add(self, kind: str, text: str, items: list | None = None) -> None:
        with self.guard:
            self.events.append({"kind": kind, "text": text, **({"items": items} if items is not None else {})})


class _Refused(Exception):
    def __init__(self, code: int):
        self.code = code


class Room:
    """One loopback server for every study under `root`. `lock` is the tool lock the caller's
    other writers share (the MCP server's `_TOOL_LOCK`), so the chat and the room never interleave."""

    def __init__(self, root: Path, lock: threading.Lock, open_browser: bool = False, provider=None,
                 key: str | None = None):
        self.root = Path(root).resolve()
        self.lock = lock
        self.open_browser = open_browser
        self.provider, self.key = provider, key     # the key is only ever used to redact
        self.chats: dict[str, _Chat] = {}
        self.token = secrets.token_urlsafe(24)
        self.changes = 0
        self._server = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def cookie(self) -> str:
        return f"naga_room_{self.port}"     # cookies ignore the port: two rooms must not collide

    def start(self) -> None:
        global ACTIVE
        room = self

        class Handler(_Handler):
            pass
        Handler.room = room
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="naga-room", daemon=True).start()
        ACTIVE = self

    def stop(self) -> None:
        global ACTIVE
        if ACTIVE is self:
            ACTIVE = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def url(self, study_id: str) -> str:
        return f"{self.address(study_id)}?t={self.token}"

    def address(self, study_id: str) -> str:
        """The room's address without its token: what may be written to disk (a study folder gets
        zipped and shared); a browser that opened the tokened link once has the cookie for it."""
        return f"http://127.0.0.1:{self.port}/s/{quote(study_id, safe='')}/"

    def folder(self, study_id: str) -> Path:
        folder = (self.root / study_id).resolve()
        if folder == self.root or not folder.is_relative_to(self.root) or not (folder / "study.json").is_file():
            raise _Refused(404)
        return folder

    def chat(self, sid: str) -> _Chat:
        return self.chats.setdefault(sid, _Chat())

    def ask(self, sid: str, text: str, voice: bool = False, what_next: bool = False) -> int | None:
        """Start one assistant turn in a thread; None while one runs. Answers the index of its first
        event. `voice`: the turn is read aloud (the model is told so). `what_next` (or a question that
        is only "what next?"): the study's rule options come first as an "options" event, and with an
        allow-listed hosted model its own checked ideas follow the turn as an "ideas" event (D728)."""
        chat = self.chat(sid)
        with chat.guard:
            if chat.running:
                return None
            chat.running = True
            chat.cancel.clear()
            start = len(chat.events)
            chat.events.append({"kind": "user", "text": text})
        what_next = what_next or bool(_WHAT_NEXT.match(text))
        options = self._options(sid) if what_next else None
        if options:     # an empty list draws nothing in the page
            chat.add("options", "From the study's numbers", options)
        if self.provider is None:
            chat.add("error", NO_MODEL)
            chat.running = False
            return start
        from nagasim import assistant, nextideas
        done = threading.Event()
        if options is not None and nextideas.enabled(self.provider):
            with chat.guard:
                chat.pending += 1
            threading.Thread(target=self._ideas, args=(sid, chat, options, done), name="naga-room-ideas",
                             daemon=True).start()
        prompt = f"{assistant.VOICE_NOTE}\n{text}" if voice else text
        threading.Thread(target=self._turn, args=(sid, chat, prompt, done), name="naga-room-chat",
                         daemon=True).start()
        return start

    def _options(self, sid: str) -> list | None:
        from nagasim import study as study_mod
        from nagasim.agent_tools.study import _options
        try:
            folder = self.folder(sid)
            return _options(folder, study_mod.load(folder))
        except (_Refused, study_mod.StudyError):
            return None

    def _ideas(self, sid: str, chat: _Chat, options: list, done: threading.Event) -> None:
        """Layer 2 (D728), beside the turn: asked at once, shown once the turn has ended so it never
        lands inside the answer. Never runs a tool."""
        from nagasim import assistant, nextideas, study as study_mod
        from nagasim.agent_tools.study import _read_layout
        try:
            folder = self.folder(sid)
            s = study_mod.load(folder)
            layout = _read_layout(folder, s["versions"][-1]["id"]) if s["versions"] else {}
            got = nextideas.ideas(self.provider, nextideas.study_facts(s), options,
                                  assistant.tool_defs(StudyAdapter.tool_names()), study_id=sid, layout=layout)
        except Exception as exc:    # a failed extra step never touches the turn's answer
            _LOG.warning("room ideas failed: %s", type(exc).__name__)
            got = {"ideas": [], "note": "no ideas: the extra step failed"}
        items = json.loads(assistant.redact(json.dumps(got["ideas"]), self.key))
        done.wait(600)
        chat.add("ideas", assistant.redact(got.get("note", ""), self.key), items)
        with chat.guard:
            chat.pending -= 1

    def _turn(self, sid: str, chat: _Chat, text: str, done: threading.Event | None = None) -> None:
        from nagasim import assistant, study as study_mod
        streamed = []
        try:
            if chat.session is None:
                def on_tool(name, failed=None):
                    if failed:
                        chat.add("tool_failed", failed)
                    else:
                        chat.add("tool", _PURPOSE.get(name, "Working"))
                adapter = StudyAdapter(self.root, sid, self.lock, on_tool=on_tool)
                system = (f"You run the simulation study in the folder {sid!r} for the person, from its live "
                          "control room. Pass that folder as `study` to every study tool. explain_kpi "
                          "(what makes up a KPI) and what_if (what a change would do, paired runs) run "
                          "on the study's last version whatever `model` you name, and save nothing: "
                          "record a change with study_step. Answer in plain words, "
                          "briefly; the page shows the numbers. The person is reading this in the room "
                          "already: never give them its link or address.\n\n"
                          + assistant.WHAT_NEXT.format(where="study status (and every study answer)",
                                                       field="options")
                          + "A turn that starts with \"" + assistant.VOICE_NOTE.split(":")[0]
                          + ":\" is spoken: keep to it.\n\n" + study_mod.skill_text())
                chat.session = assistant.AssistantSession(
                    adapter, self.provider, system=system, select_tools=lambda kind, extra: adapter.names)

            def delta(t):
                streamed.append(t)
                chat.add("text", assistant.redact(t, self.key))
            result = chat.session.run_turn(text, on_text_delta=delta, should_cancel=chat.cancel.is_set)
            if result.text and not streamed:        # a provider that answers per round, not streamed
                chat.add("text", assistant.redact(result.text, self.key))
            if result.error:
                chat.add("error", assistant.redact(result.error, self.key))
            elif result.cancelled:
                chat.add("error", "Stopped.")
            else:
                if result.hit_round_cap:
                    chat.add("text", " (I stopped at my limit of tool rounds; say carry on to go further.)")
                chat.add("done", "")
        except Exception as exc:    # the panel must hear of any failure, and the room keep serving
            _LOG.warning("room assistant turn failed: %s", type(exc).__name__)
            chat.add("error", assistant.redact(f"{type(exc).__name__}: {exc}", self.key))
        finally:
            self.changes += 1
            chat.running = False
            if done is not None:
                done.set()

    def rev(self, folder: Path) -> str:
        try:
            mtime = (folder / "study.json").stat().st_mtime_ns
        except OSError:
            mtime = 0
        return f"{mtime}-{self.changes}"


class _Handler(http.server.BaseHTTPRequestHandler):
    room: Room
    server_version = "naga-room"
    sys_version = ""

    def log_message(self, fmt, *args):     # the request line holds the token: path only
        _LOG.info("room %s %s", self.command, getattr(self, "path", "").split("?", 1)[0])

    # -- plumbing -------------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), "Referrer-Policy": "no-referrer",
                   "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **(extra or {})}
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json")

    def _check(self) -> tuple[str, list[str], dict]:
        """(study id, the route's parts, query) of a request that proved it is the person's page."""
        room = self.room
        if self.headers.get("Host") not in (f"127.0.0.1:{room.port}", f"localhost:{room.port}"):
            raise _Refused(403)
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        given = (query.get("t") or [""])[0]
        if not given:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:
                pass
            given = jar[room.cookie].value if room.cookie in jar else ""
        if not secrets.compare_digest(given.encode(), room.token.encode()):
            raise _Refused(403)
        if self.command == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json" or self.headers.get("Origin") != f"http://{self.headers['Host']}":
                raise _Refused(403)
        segs = parts.path.split("/")
        if len(segs) < 4 or segs[0] != "" or segs[1] != "s":
            raise _Refused(404)
        return unquote(segs[2]), segs[3:], query

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _Refused(400) from None
        if n < 0:       # rfile.read(-1) waits for EOF and would hold the thread
            raise _Refused(400)
        if n > MAX_BODY:
            raise _Refused(413)
        self._unread = 0
        try:
            data = json.loads(self.rfile.read(n) or b"null")
        except ValueError:
            raise _Refused(400) from None
        if not isinstance(data, dict):
            raise _Refused(400)
        return data

    def _drain(self) -> None:
        """Read a refused request's body before answering: closing a socket with unread data makes
        Windows reset the connection, and the client then sees the reset instead of the 403."""
        n = self._unread
        self._unread = 0
        if 0 < n <= MAX_BODY:
            self.rfile.read(n)

    def _handle(self) -> None:
        try:
            self._unread = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._unread = 0
        try:
            sid, rest, query = self._check()
            folder = self.room.folder(sid)
            route = "/".join(rest)
            if self.command == "GET":
                self._get(sid, folder, route, rest, query)
            else:
                self._post(sid, folder, route)
        except _Refused as exc:
            self._drain()
            self._send(exc.code, b"", "text/plain")
        except Exception:
            _LOG.warning("room request failed", exc_info=True)
            self._send(500, b"", "text/plain")

    do_GET = do_POST = do_HEAD = _handle

    # -- routes ---------------------------------------------------------------------------
    def _get(self, sid: str, folder: Path, route: str, rest: list[str], query: dict) -> None:
        from nagasim import studyapp, studypage
        if route == "":
            cookie = f"{self.room.cookie}={self.room.token}; HttpOnly; SameSite=Strict; Path=/"
            self._send(200, studyapp.room_html().encode("ascii"), "text/html; charset=utf-8",
                       {"Set-Cookie": cookie, "Content-Security-Policy": _SHELL_CSP})
        elif route == "api/page":
            rev = self.room.rev(folder)
            self._json({"html": studypage.render(folder, study_id=sid, in_room=True), "rev": rev})
        elif route == "api/rev":
            self._json({"rev": self.room.rev(folder)})
        elif route == "api/chat":
            try:
                since = max(0, int((query.get("since") or ["0"])[0]))
            except ValueError:
                raise _Refused(400) from None
            chat = self.room.chat(sid)
            with chat.guard:
                self._json({"events": chat.events[since:], "running": chat.running,
                            "pending": chat.pending > 0})
        elif rest[0] == "files" and len(rest) > 1:
            path = _confined(folder, rest[1:])
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            # sandbox: a study file (a report, an SVG) never runs script with the room's origin
            self._send(200, path.read_bytes(), ctype, {"Content-Security-Policy": "sandbox"})
        else:
            raise _Refused(404)

    def _post(self, sid: str, folder: Path, route: str) -> None:
        if route == "api/tool":
            self._json(self._tool(sid, self._body()))
        elif route == "api/chat":
            body = self._body()
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise _Refused(400)
            turn = self.room.ask(sid, text.strip()[:20000], voice=body.get("voice") is True,
                                 what_next=body.get("what_next") is True)
            if turn is None:
                raise _Refused(409)
            self._json({"turn": turn})
        elif route == "api/chat/stop":
            self._body()
            self.room.chat(sid).cancel.set()
            self._json({"ok": True})
        else:
            raise _Refused(404)

    def _tool(self, sid: str, body: dict) -> dict:
        from nagasim import agent_tools
        name, args = body.get("name"), body.get("arguments") or {}
        if not isinstance(args, dict):
            raise _Refused(400)
        args = {k: v for k, v in args.items() if k != "study"}
        approve = name == "study_update" and args == {"plan_approved": True}
        if name not in DIRECT and not approve:
            raise _Refused(403)
        with self.room.lock, agent_tools.python_consent_dropped():
            out = agent_tools.call(name, study=sid, **args)
            if not agent_tools.TOOLS[name].read_only:   # a bump reloads the page: it would wipe a picture just shown
                self.room.changes += 1
        return out


def _confined(folder: Path, segs: list[str]) -> Path:
    """A file inside the study folder, or 404: no empty, dot, drive or backslash segment, and the
    resolved path (symlinks followed) must stay inside."""
    names = [unquote(s) for s in segs]
    if any(n in ("", ".", "..") or ":" in n or "\\" in n or "/" in n for n in names):
        raise _Refused(404)
    path = folder.joinpath(*names).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise _Refused(404)
    return path


def main(argv=None) -> int:
    """`naga room <study folder> [--root DIR] [--no-browser]`: serve the study's live control room
    until Ctrl+C."""
    from nagasim import agent_tools, study as study_mod
    parser = argparse.ArgumentParser(prog="naga room", description=(
        "The live control room of a study in your browser, on this machine only (127.0.0.1)."))
    parser.add_argument("folder", help="the study folder (it holds study.json)")
    parser.add_argument("--root", default=None,
                        help="workspace root the study tools are confined to (default: the folder's parent)")
    parser.add_argument("--no-browser", action="store_true", help="print the address, do not open a browser")
    args = parser.parse_args(argv)
    folder = Path(args.folder).resolve()
    root = Path(args.root).resolve() if args.root else folder.parent
    if not (folder / "study.json").is_file() or not folder.is_relative_to(root) or folder == root:
        sys.stderr.write(f"naga room: no study in {folder} under {root}\n")
        return 2
    agent_tools.drop_python_consent()       # D186: the buttons never run a model's Python
    agent_tools.use_workspace(agent_tools.Workspace(root))
    sid = folder.relative_to(root).as_posix()
    provider, key = provider_from_env()
    room = Room(root, threading.Lock(), provider=provider, key=key)
    room.start()
    try:
        study_mod.claim(folder, room=room.address(sid))
    except study_mod.StudyError as exc:
        room.stop()
        held = _held(folder)
        if held.get("room"):
            print(f"This study's room is already running at {held['room']}: open the link the first "
                  "naga room printed (its key is not stored on disk).")
            return 0
        sys.stderr.write(f"naga room: {exc} {exc.hint}\n")
        return 1
    url = room.url(sid)
    # flush: a redirected stdout is block-buffered and the address must reach whoever reads it now
    print(f"Control room of {sid}: {url}", flush=True)
    if provider is None:
        print("The assistant panel is off: " + NO_MODEL, flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        room.stop()
    return 0


def _held(folder: Path) -> dict:
    from nagasim import study as study_mod
    try:
        data = json.loads((folder / study_mod.WRITER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
