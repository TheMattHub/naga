// Drives a viewer page in headless Chrome or Edge over the DevTools protocol, with
// node's own WebSocket and fetch (node 22) and no package. tests/test_viewer.py runs it:
//
//   node viewer_drive.cjs BROWSER.exe PAGE.html PROFILE_DIR 'STEPS as JSON'
//
// A step is {"wait": "a regular expression the status line must match", "limit": ms} or
// {"eval": "an expression"}; the answers are printed as one JSON object with the
// addresses asked for (anything but file:, data: and blob:) by the page, its frames and
// EVERY worker: each worker is attached before it runs a line (Target.setAutoAttach,
// held at start), and a WebSocket counts as an address asked. What the protocol does not
// report (WebRTC, a connection with no request) is pinned by a static test of the
// page's own scripts instead (tests/test_viewer.py).
const { spawn } = require("child_process"), path = require("path"), fs = require("fs");
const [exe, page, profile, steps] = process.argv.slice(2);
// Not an address asked of anyone: the page's own file and bytes, and the browser's own
// built-in pages (Edge now and then attaches one of its `edge:` pages as a child target).
const LOCAL = /^(file|data|blob|about|edge|chrome|devtools):/;

(async () => {
  // Port 0: the browser takes a free port and writes it to DevToolsActivePort in its
  // profile. A port of our own choosing collided under pytest -n auto: two tests drove
  // one browser and read each other's page (#832).
  const browser = spawn(exe, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"], { stdio: "ignore" });
  const out = { answers: [], asked: [], console: [], attached: 0 };
  try {
    let target;
    for (let i = 0; i < 150 && !target; i += 1) {
      await new Promise(r => setTimeout(r, 200));
      try {
        const port = fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0].trim();
        target = (await (await fetch("http://127.0.0.1:" + port + "/json")).json()).find(t => t.type === "page");
      }
      catch (e) { /* the browser is not up yet */ }
    }
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0;
    const pending = {};
    ws.onmessage = m => {
      const d = JSON.parse(m.data);
      if (d.id) { pending[d.id](d.result || d.error); }
      else if (d.method === "Network.requestWillBeSent" && !LOCAL.test(d.params.request.url)) { out.asked.push(d.params.request.url); }
      else if (d.method === "Network.webSocketCreated") { out.asked.push(d.params.url); }
      else if (d.method === "Log.entryAdded") { out.console.push(d.params.entry.text.slice(0, 300)); }
      else if (d.method === "Target.attachedToTarget") { watch(d.params.sessionId); }
    };
    const send = (method, params, sessionId) => new Promise(r => {
      id += 1; pending[id] = r;
      ws.send(JSON.stringify(Object.assign({ id, method, params: params || {} }, sessionId ? { sessionId } : {})));
    });
    // A worker (or a frame of another process) is held until its requests are watched,
    // and its own workers are attached the same way.
    const watch = sessionId => {
      out.attached += 1;
      send("Network.enable", {}, sessionId);
      send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, sessionId);
      send("Runtime.runIfWaitingForDebugger", {}, sessionId);
    };
    const value = async expression => {
      const got = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      return got.result ? got.result.value : "EVALUATION FAILED " + JSON.stringify(got).slice(0, 300);
    };
    await send("Network.enable"); await send("Log.enable");
    await send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
    await send("Page.navigate", { url: "file:///" + path.resolve(page).replace(/\\/g, "/") });
    // The page's own markup is there before the first step (its 40 MB of data follow it).
    for (let i = 0; i < 300 && (await value("!!(document.body && document.getElementById('status'))")) !== true; i += 1) {
      await new Promise(r => setTimeout(r, 100));
    }
    for (const step of JSON.parse(steps)) {
      if (step.eval) { out.answers.push(await value(step.eval)); continue; }
      const until = Date.now() + (step.limit || 90000), wanted = new RegExp(step.wait);
      let text = "";
      while (Date.now() < until && !wanted.test(text)) {
        await new Promise(r => setTimeout(r, 200));
        text = String(await value("(document.getElementById('status') || {}).textContent || ''"));
      }
      out.answers.push(text);
      if (!wanted.test(text)) { out.failed = "waited for /" + step.wait + "/, the status is: " + text; break; }
    }
    ws.close();
  } catch (error) { out.failed = String(error && error.stack || error).slice(0, 600); }
  browser.kill();
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
