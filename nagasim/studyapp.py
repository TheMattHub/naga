"""The control room shell: one static page that shows `studypage.render`'s page and adds the buttons.

Two homes, one client. In a chat that renders MCP Apps (`HTML`) it is a plain JSON-RPC-over-postMessage
client (no library): it asks the host to call the study tools, and the assistant buttons post a
`ui/message` into the conversation. In the browser room (`room_html()`, served by `nagasim.studyroom`)
the same client talks to the loopback server with `fetch` and polls it for changes. The two differ only
in the transport chunk, so the host page holds no address and the room page no postMessage.

The returned page is parsed with DOMParser and only its <style> and <main> are imported; scripts and
on* attributes are dropped even though `studypage.render` never emits them. Every button is drawn
from the page's data-* marks; every message shown uses textContent. The host's default CSP allows
no network, and the host variant needs none."""
URI = "ui://naga/study.html"

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Study control room</title>
<style>
body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0}
#naga-msg{padding:8px 16px;font-weight:600;color:#7a1f1f}#naga-msg:empty{display:none}
#actions{max-width:1100px;margin:0 auto;padding:0 16px}
#actions input{width:40%;padding:4px 8px;margin:0 8px}
.naga-err{color:#7a1f1f;font-weight:600;margin-left:8px}
.naga-bar{max-width:1100px;margin:0 auto;padding:4px 16px}.naga-bar button{margin-right:6px}
button{cursor:pointer;padding:3px 10px;margin:2px 4px 2px 0}button:disabled{cursor:default}
img.naga-flow{display:block;max-width:100%;margin:6px 0}
/*STYLE*/
</style></head><body>
<div id="naga-msg" role="status"></div>
<div id="page"></div>
/*BODY*/
<script>
(function () {
  var study = null, busy = false;
  var statusEl = document.getElementById("naga-msg"), pageEl = document.getElementById("page");
  // Open state of the panels, in memory only (lost on a reload of the chat). Keyed by the panel id,
  // or by its position for the nested ones without an id. ponytail: position keys can shift if the
  // page's structure changes between two renders; add ids to the nested panels if that happens.
  var openState = Object.create(null);

  function panels() { return Array.prototype.slice.call(pageEl.querySelectorAll("details")); }
  function panelKey(d) { return d.id || "#" + panels().indexOf(d); }
  pageEl.addEventListener("toggle", function (ev) { openState[panelKey(ev.target)] = ev.target.open; }, true);
  function restorePanels() {
    panels().forEach(function (d) { var k = panelKey(d); if (k in openState) { d.open = openState[k]; } });
  }
  function button(text, onclick) {
    var b = document.createElement("button");
    b.textContent = text;
    b.disabled = busy;
    b.onclick = onclick;
    return b;
  }
  function toolbar() {
    var bar = document.createElement("div");
    bar.className = "naga-bar";
    [["Expand all", true], ["Collapse all", false]].forEach(function (p) {
      bar.appendChild(button(p[0], function () { panels().forEach(function (d) { d.open = p[1]; }); }));
    });
    pageEl.insertBefore(bar, pageEl.firstChild);
  }
  function say(text) { statusEl.textContent = text || ""; }
  // One thing at a time: every button of the page is off while a call or an assistant turn runs.
  function setBusy(on) {
    busy = on;
    pageEl.querySelectorAll("button").forEach(function (b) { b.disabled = on; });
  }

  function insert(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("script").forEach(function (s) { s.remove(); });
    doc.querySelectorAll("*").forEach(function (el) {
      Array.prototype.slice.call(el.attributes).forEach(function (a) {
        var v = a.value.replace(/\s+/g, "").toLowerCase();
        if (a.name.slice(0, 2) === "on" || v.indexOf("javascript:") === 0) { el.removeAttribute(a.name); }
      });
    });
    var y = window.scrollY;
    pageEl.textContent = "";
    doc.querySelectorAll("style, main").forEach(function (el) {
      pageEl.appendChild(document.importNode(el, true));
    });
    var cover = pageEl.querySelector("[data-study]");
    if (!study && cover) { study = cover.getAttribute("data-study"); }
    restorePanels();
    toolbar();
    decorate();
    window.scrollTo(0, y);
  }

  function note(b, text) {
    // Beside the button: the message line may be scrolled out of view.
    var n = b.nextSibling;
    if (!n || n.className !== "naga-err") {
      n = document.createElement("span");
      n.className = "naga-err";
      b.parentNode.insertBefore(n, b.nextSibling);
    }
    n.textContent = text;
  }

  // Run a tool the person asked for, then show the refreshed page; an error stays in the status line.
  function act(b, run, after) {
    setBusy(true);
    say("Working...");
    run().then(function (out) {
      setBusy(false);
      return load().then(function () { say(""); if (after) { after(out); } });
    }).catch(function (e) {
      setBusy(false);
      say(e.message);
      if (b.parentNode) { note(b, e.message); }
    });
  }
  function ask(text) {
    setBusy(true);
    Promise.resolve().then(function () { return askAssistant(text); })
      .catch(function (e) { say(e.message); })
      .then(function () { setBusy(false); });
  }
  function picture(data, alt) {
    var img = document.createElement("img");
    img.className = "naga-flow";
    img.alt = alt;
    img.src = "data:image/png;base64," + data;
    return img;
  }

  function flowchart(el, id) {
    var b = button("Show flowchart", function () {
      setBusy(true);
      callTool("study_view", { study: study, version: id, format: "png" }).then(function (r) {
        setBusy(false);
        var old = el.querySelector("img.naga-flow");
        if (old) { old.remove(); }
        if (!r.image) { note(b, r.note || "No image here; the version's details list the blocks."); return; }
        el.appendChild(picture(r.image.data, "Flowchart of " + id));
      }).catch(function (e) { setBusy(false); note(b, e.message); });
    });
    return b;
  }

  // Each deliverable's "Make it": the tool that makes it.
  var MAKERS = {
    report: function () { return callTool("study_report", { study: study }); },
    view: function () { return callTool("study_view", { study: study, format: "png" }); },
    package: function () { return callTool("study_package", { study: study }); },
    sensitivity: function () { return callTool("study_sensitivity", { study: study }); },
    extensions: function () { return callTool("study_extensions", { study: study }); },
    animation: function () { return callTool("study_animation", { study: study }); },
    writeup: function () { return callTool("study_writeup", { study: study }); },
    slides: function () { return callTool("study_slides", { study: study }); }
  };
  function made(kind, out) {
    if (kind === "view" && out && out.image) {
      var li = pageEl.querySelector('[data-deliverable="view"]');
      if (li) { li.appendChild(picture(out.image.data, "Flowchart of the latest version")); }
    } else if (kind === "extensions" && out && out.candidates) {
      say(out.candidates.length ? "Candidates: " + out.candidates.map(function (c) { return c.label; }).join(", ")
                                : "No candidate extension fits this model.");
    }
  }

  function decorate() {
    var header = pageEl.querySelector("header");
    var accepted = header && header.hasAttribute("data-accepted");
    pageEl.querySelectorAll("[data-version]").forEach(function (v) {
      var id = v.getAttribute("data-version");
      v.appendChild(flowchart(v, id));
      if (!v.hasAttribute("data-latest")) {
        var r = button("Revert to " + id, function () {
          act(r, function () {
            return callTool("study_revert", { study: study, version: id, note: "reverted from the control room" });
          });
        });
        v.appendChild(r);
      }
    });
    var box = pageEl.querySelector("#actions");
    if (box) {
      if (header && header.hasAttribute("data-plan-pending")) {
        var ap = button("Approve plan", function () {
          act(ap, function () { return callTool("study_update", { study: study, plan_approved: true }); });
        });
        box.appendChild(ap);
      }
      box.appendChild(button("Next iteration", function () {
        ask("Carry on with the study: the next open item.");
      }));
      // The verdict on the latest version sits at the top, where it is seen: the version rows are closed.
      var latest = pageEl.querySelector("[data-latest]");
      if (latest && !accepted) {
        var vid = latest.getAttribute("data-version");
        var why = document.createElement("input");
        why.placeholder = "A note, or what is missing";
        var yes = button("This answers my request", function () {
          var text = "the person confirmed it answers the request" + (why.value.trim() ? ": " + why.value.trim() : "");
          act(yes, function () { return callTool("study_accept", { study: study, reason: text }); });
        });
        var no = button("Not quite", function () {
          if (!why.value.trim()) { why.focus(); say("Say what is missing, then press Not quite again."); return; }
          ask("Not quite (" + vid + "): " + why.value.trim()
              + ". Explain how to sharpen the request, then build the next version.");
        });
        box.appendChild(why);
        box.appendChild(yes);
        box.appendChild(no);
      }
    }
    pageEl.querySelectorAll("[data-deliverable]").forEach(function (li) {
      var kind = li.getAttribute("data-deliverable"), path = li.getAttribute("data-path");
      var mk = button(path ? "Make again" : "Make it", function () {
        act(mk, MAKERS[kind], function (out) { made(kind, out); });
      });
      li.appendChild(mk);
      if (path) { li.appendChild(button("Open", function () { openFile(path); })); }
    });
    var sens = pageEl.querySelector("#sensitivity[data-empty] .scroll");
    if (sens) {
      var rs = button("Run sensitivity", function () { act(rs, MAKERS.sensitivity); });
      sens.appendChild(rs);
    }
  }

/*TRANSPORT*/})();
</script></body></html>
"""

_HOST = r"""  // Host transport: JSON-RPC over postMessage with the MCP Apps host.
  var pending = {}, nextId = 1, loaded = false;
  function send(msg) { msg.jsonrpc = "2.0"; parent.postMessage(msg, "*"); }
  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      send({ id: id, method: method, params: params });
    });
  }
  // The answer of one of our tools: structuredContent.result, else the first text item as JSON.
  function callTool(name, args) {
    return request("tools/call", { name: name, arguments: args }).then(function (r) {
      var out, text = "";
      var sc = r.structuredContent;
      if (sc && sc.result !== undefined) { out = sc.result; }
      else {
        var first = (r.content || []).filter(function (c) { return c.type === "text"; })[0];
        text = first ? first.text : "";
        try { out = JSON.parse(text); } catch (e) { out = null; }
      }
      if (out && out.error) { throw new Error(out.error.message || "The tool refused."); }
      if (r.isError) { throw new Error(text || "The tool failed."); }
      return out;
    });
  }
  function load() {
    loaded = true;
    return callTool("study_page_html", { study: study }).then(function (r) { insert(r.html); });
  }
  // The host's own assistant does the work; without ui/message (an error, or no answer in 3 s)
  // the text goes to the clipboard for the person to paste.
  function askAssistant(text) {
    var late = new Promise(function (_, reject) {
      setTimeout(function () { reject(new Error("no answer")); }, 3000);
    });
    return Promise.race([
      request("ui/message", { role: "user", content: [{ type: "text", text: text }] }), late
    ]).then(function (r) {
      if (r && r.isError) { throw new Error("refused"); }
      say("Sent to the chat.");
    }).catch(function () {
      return navigator.clipboard.writeText(text).then(function () {
        say("Copied. Paste this into the chat.");
      }, function () { say("Paste this into the chat: " + text); });
    });
  }
  // In the chat a file cannot be opened: name it (the browser room opens it).
  function openFile(path) { say("The file is " + path + " in the study folder."); }

  window.addEventListener("message", function (ev) {
    var m = ev.data;
    if (ev.source !== parent || !m || m.jsonrpc !== "2.0") { return; }
    if (m.method === undefined) {
      var p = pending[m.id];
      if (!p) { return; }
      delete pending[m.id];
      if (m.error) { p.reject(new Error(m.error.message || "The host refused.")); } else { p.resolve(m.result); }
    } else if (m.method === "ui/notifications/tool-input") {
      study = (m.params && m.params.arguments && m.params.arguments.study) || study;
      if (!loaded) { load().catch(function (e) { say(e.message); }); }
    } else if (m.method === "ui/notifications/tool-result") {
      if (!loaded) { load().catch(function (e) { say(e.message); }); }
    }
  });

  request("ui/initialize", {
    protocolVersion: "2026-01-26",
    appInfo: { name: "naga-study", version: "1" },
    appCapabilities: {}
  }).then(function () {
    send({ method: "ui/notifications/initialized", params: {} });
    // The host sizes the frame from these (spec: ui/notifications/size-changed, SHOULD).
    var root = document.documentElement, last = "";
    new ResizeObserver(function () {
      var size = root.scrollWidth + "x" + root.scrollHeight;
      if (size === last) { return; }
      last = size;
      send({ method: "ui/notifications/size-changed",
             params: { width: root.scrollWidth, height: root.scrollHeight } });
    }).observe(document.body);
  }).catch(function (e) { say(e.message); });
"""

_ROOM = r"""  // Room transport: fetch to the loopback server that served this page (same origin). The token
  // is in the address on the first load only (removed below); then an HttpOnly cookie carries it. The
  // page it loads (api/page) holds no token address; the script does read location.search once.
  var rev = null;
  // The token came in the address once; the cookie carries it now, so it leaves the address bar.
  if (location.search) { history.replaceState(null, "", location.pathname); }
  function answer(r) {
    return r.json().catch(function () { return {}; }).then(function (out) {
      if (!r.ok && !(out && out.error)) { throw new Error("The room refused (" + r.status + ")."); }
      return out;
    });
  }
  function post(path, body) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify(body), credentials: "same-origin" }).then(answer);
  }
  function getJson(path) { return fetch(path, { credentials: "same-origin" }).then(answer); }
  function callTool(name, args) {
    return post("./api/tool", { name: name, arguments: args }).then(function (out) {
      if (out && out.error) { throw new Error(out.error.message || "The tool refused."); }
      return out;
    });
  }
  function load() {
    return getJson("./api/page").then(function (r) { rev = r.rev; insert(r.html); });
  }
  function askAssistant(text) { return chatSend(text); }
  function openFile(path) {
    window.open("./files/" + path.split("/").map(encodeURIComponent).join("/"), "_blank", "noopener");
  }
  // The chat or the room's assistant may change the study at any time: reload on a new rev, also
  // while a turn runs (the page follows the assistant; its buttons stay off until the turn ends).
  setInterval(function () {
    getJson("./api/rev").then(function (r) {
      if (r.rev !== rev) { load().catch(function (e) { say(e.message); }); }
    }).catch(function () {});
  }, 2000);
/*CHAT*/
  load().catch(function (e) { say(e.message); });
"""

# The room's assistant panel (room variant only): a message list, a box, Send and Stop, a microphone
# when the browser has speech recognition, and an optional read-aloud. No library, no server code
# for the voice: the browser's Web Speech API.
_CHAT_STYLE = """#naga-chat{max-width:1100px;margin:8px auto;padding:8px 16px;border:1px solid #dde1e6;border-radius:8px;
 background:#fff}
#naga-chat h2{font-size:15px;margin:0 0 4px}#chat-log{max-height:40vh;overflow:auto;margin:4px 0}
#chat-log p{margin:4px 0;white-space:pre-wrap}.chat-user{font-weight:600}.chat-tool{color:#666;font-size:13px}
.chat-error{color:#7a1f1f;font-weight:600}#chat-in{width:100%;box-sizing:border-box;min-height:3em}
.chat-note{color:#666;font-size:13px;margin-left:6px}
@media (min-width:1100px){body{display:grid;grid-template-columns:minmax(0,1fr) 380px;align-items:start}
#naga-msg{grid-column:1/-1;grid-row:1}#page{grid-column:1;grid-row:2}
#naga-chat{grid-column:2;grid-row:2;position:sticky;top:8px;max-height:calc(100vh - 32px);overflow:auto;
 box-sizing:border-box;max-width:none;width:calc(100% - 16px);margin:8px}#chat-log{max-height:none}}"""

_CHAT_BODY = """<section id="naga-chat" aria-label="Assistant">
<h2>Assistant</h2>
<div id="chat-log" aria-live="polite"></div>
<textarea id="chat-in" placeholder="Ask the assistant about the study, or tell it what to do next"></textarea>
<div><button id="chat-send">Send</button><button id="chat-stop" hidden>Stop</button>
<button id="chat-mic" hidden>Speak</button><span id="chat-mic-note" class="chat-note" hidden>Your browser may send speech to its vendor's service.</span>
<label class="chat-note"><input type="checkbox" id="chat-aloud"> Read answers aloud</label></div>
</section>"""

_CHAT = r"""  var since = 0, turn = null;
  var logEl = document.getElementById("chat-log"), inEl = document.getElementById("chat-in");
  var sendEl = document.getElementById("chat-send"), stopEl = document.getElementById("chat-stop");
  var micEl = document.getElementById("chat-mic"), aloudEl = document.getElementById("chat-aloud");
  var answerEl = null, lastAnswer = null;
  function line(cls, text) {
    var p = document.createElement("p");
    p.className = cls;
    p.textContent = text;
    logEl.appendChild(p);
    logEl.scrollTop = logEl.scrollHeight;
    return p;
  }
  function show(ev) {
    if (ev.kind === "user") { answerEl = null; line("chat-user", "You: " + ev.text); }
    else if (ev.kind === "text") {
      if (!answerEl) { answerEl = lastAnswer = line("chat-answer", ""); }
      answerEl.textContent += ev.text;
      logEl.scrollTop = logEl.scrollHeight;
    } else if (ev.kind === "tool") { answerEl = null; line("chat-tool", ev.text + "..."); }
    else if (ev.kind === "error") { answerEl = null; line("chat-error", ev.text); }
    else if (ev.kind === "done") {
      if (aloudEl.checked && lastAnswer && window.speechSynthesis) {
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(lastAnswer.textContent));
      }
      answerEl = null;
    }
  }
  function running(on) {
    sendEl.disabled = on;
    stopEl.hidden = !on;
    setBusy(on);
  }
  // Poll the turn's events every 700 ms until it ends; resolves then.
  function follow() {
    return new Promise(function (resolve) {
      function tick() {
        getJson("./api/chat?since=" + since).then(function (r) {
          r.events.forEach(show);
          since += r.events.length;
          if (r.running) { running(true); setTimeout(tick, 700); } else { running(false); resolve(); }
        }).catch(function (e) { line("chat-error", e.message); running(false); resolve(); });
      }
      tick();
    });
  }
  function chatSend(text) {
    if (turn) { return turn; }
    running(true);
    turn = post("./api/chat", { text: text }).then(function (out) {
      if (out && out.error) { throw new Error(out.error.message || "The assistant refused."); }
      return follow();
    }).catch(function (e) { line("chat-error", e.message); running(false); })
      .then(function () { turn = null; });
    return turn;
  }
  sendEl.onclick = function () {
    var text = inEl.value.trim();
    if (!text) { return; }
    inEl.value = "";
    chatSend(text);
  };
  stopEl.onclick = function () { post("./api/chat/stop", {}).catch(function () {}); };
  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (Recognition) {
    micEl.hidden = false;
    document.getElementById("chat-mic-note").hidden = false;
    micEl.onclick = function () {
      var rec = new Recognition();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = true;
      rec.onresult = function (ev) {
        var text = "", final = false;
        for (var i = 0; i < ev.results.length; i++) {
          text += ev.results[i][0].transcript;
          final = ev.results[i].isFinal;
        }
        inEl.value = text;
        if (final) { sendEl.onclick(); }
      };
      rec.onerror = function () { say("The microphone did not work here."); };
      rec.start();
    };
  }
  // A reload of the page keeps the conversation: show what the room already holds, and follow a
  // turn that is still running.
  follow();
"""


HTML = _TEMPLATE.replace("/*TRANSPORT*/", _HOST).replace("/*STYLE*/\n", "").replace("/*BODY*/\n", "")


def room_html() -> str:
    """The shell for the browser room: the same client over fetch, polling for changes, with the
    room's assistant panel."""
    return (_TEMPLATE.replace("/*TRANSPORT*/", _ROOM.replace("/*CHAT*/\n", _CHAT))
            .replace("/*STYLE*/", _CHAT_STYLE).replace("/*BODY*/", _CHAT_BODY))
