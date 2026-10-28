import contextlib
import io
from pathlib import Path

from nagasim import build as nb, cli

# A small model to share: a queue with one server.
m = nb.Model(time_limit=240, warmup=0, replications=3)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25)) >> m.dispose()
m.save("model.json")

# The recording: `naga run` plays the first replication and writes it as one HTML file.
print("$ naga run model.json --player queue.html")
shown = io.StringIO()
with contextlib.redirect_stdout(shown):         # the command also prints the KPI table; this page does not need it
    code = cli.main(["run", "model.json", "--player", "queue.html"])
print(shown.getvalue().splitlines()[-1])

page = Path("queue.html")
text = page.read_text(encoding="utf-8")
print(f"exit code {code}; {page.name} is {page.stat().st_size / 1000:.0f} KB")

# What the file asks of the browser, and what it leaves out.
print("loads a script from elsewhere:", "<script src" in text)
print("has a content-security-policy:", "Content-Security-Policy" in text)
print("shows the resource name:", "Server" in text)
