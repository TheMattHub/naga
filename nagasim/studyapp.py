"""The control room as an MCP App: one static page for assistants that render `ui://` resources.

The page is a plain JSON-RPC-over-postMessage client (no library). It asks the host to call
`study_page_html`, inserts the returned page, and adds the buttons. The returned HTML is
parsed with DOMParser and only its <style> and <main> are imported; scripts and on* attributes
are dropped even though `studypage.render` never emits them. Every message shown uses
textContent. The host's default CSP allows no network, and none is needed."""
URI = "ui://naga/study.html"

HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Study control room</title>
<style>
body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0}
#naga-msg{padding:8px 16px;font-weight:600;color:#7a1f1f}#naga-msg:empty{display:none}
#actions{max-width:1100px;margin:0 auto;padding:0 16px}
#actions input{width:60%;padding:4px 8px;margin-right:8px}
.naga-err{color:#7a1f1f;font-weight:600;margin-left:8px}
button{cursor:pointer;padding:3px 10px}button:disabled{cursor:default}
</style></head><body>
<div id="naga-msg" role="status"></div>
<div id="page"></div>
<script>
(function () {
  var pending = {}, nextId = 1, study = null, loaded = false;
  var statusEl = document.getElementById("naga-msg"), pageEl = document.getElementById("page");

  function send(msg) { msg.jsonrpc = "2.0"; parent.postMessage(msg, "*"); }
  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      send({ id: id, method: method, params: params });
    });
  }
  function say(text) { statusEl.textContent = text || ""; }

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

  function insert(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("script").forEach(function (s) { s.remove(); });
    doc.querySelectorAll("*").forEach(function (el) {
      Array.prototype.slice.call(el.attributes).forEach(function (a) {
        if (a.name.slice(0, 2) === "on") { el.removeAttribute(a.name); }
      });
    });
    pageEl.textContent = "";
    doc.querySelectorAll("style, main").forEach(function (el) {
      pageEl.appendChild(document.importNode(el, true));
    });
    var cover = pageEl.querySelector("[data-study]");
    if (!study && cover) { study = cover.getAttribute("data-study"); }
    decorate();
  }

  function load() {
    loaded = true;
    return callTool("study_page_html", { study: study }).then(function (r) { insert(r.html); });
  }

  // Run a tool the person asked for, then show the refreshed page; an error stays in the status line.
  function act(buttons, run) {
    buttons.forEach(function (b) { b.disabled = true; });
    say("Working...");
    run().then(load).then(function () { say(""); }).catch(function (e) {
      say(e.message);
      // Also beside the button: the message line may be scrolled out of view.
      var b = buttons[0], note = b.nextSibling;
      if (!note || note.className !== "naga-err") {
        note = document.createElement("span");
        note.className = "naga-err";
        b.parentNode.insertBefore(note, b.nextSibling);
      }
      note.textContent = e.message;
      buttons.forEach(function (b) { b.disabled = false; });
    });
  }

  function decorate() {
    // Newest first: the first version is the current model, so it gets no revert button.
    pageEl.querySelectorAll("[data-version]").forEach(function (v, i) {
      if (i === 0) { return; }
      var id = v.getAttribute("data-version");
      var b = document.createElement("button");
      b.textContent = "Revert to " + id;
      b.onclick = function () {
        act([b], function () {
          return callTool("study_revert", { study: study, version: id, note: "reverted from the control room" });
        });
      };
      v.appendChild(b);
    });
    var box = pageEl.querySelector("#actions");
    if (!box) { return; }
    var reason = document.createElement("input");
    reason.placeholder = "Why is the study good enough?";
    var accept = document.createElement("button");
    accept.textContent = "Accept as it stands";
    accept.disabled = true;
    reason.oninput = function () { accept.disabled = reason.value.trim() === ""; };
    accept.onclick = function () {
      act([accept], function () {
        return callTool("study_accept", { study: study, reason: reason.value.trim() });
      });
    };
    box.appendChild(reason);
    box.appendChild(accept);
  }

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
})();
</script></body></html>
"""
