import http.server
import json
import threading

from nagasim import api, build as nb, databind, netguard

# 1. A stand-in for the plant: a small web server on this machine that answers with what the line
#    reports right now. In a study this is your MES or your line controller.
reading = [{"line": "press", "arrivals_per_min": 1.0, "service_per_min": 1.25}]


class Plant(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(reading).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Plant)
threading.Thread(target=server.serve_forever, daemon=True).start()

# 2. The connections file is yours. It holds the address; the model file holds only a name.
with open("connections.json", "w", encoding="utf-8") as f:
    json.dump({"format": 1, "connections": {"plant": {
        "kind": "rest", "url": f"http://127.0.0.1:{server.server_port}", "private": True,
        "root": "/"}}}, f)
policy = netguard.NetworkPolicy(connections=netguard.load_connections("connections.json"))

# 3. The model reads two of its parameters from the plant instead of holding them.
m = nb.Model(time_limit=2000, warmup=200)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.0))
arrive >> serve >> m.dispose()
layout = m.to_dict()
layout["data_sources"] = [{"name": "line", "kind": "rest", "connection": "plant", "location": "/",
                           "options": {"key": "line"}}]
layout["data_bindings"] = [
    {"name": "arrivals", "source": "line", "column": "arrivals_per_min", "transform": "value",
     "target": f"blocks.{arrive.name}.params.dist_params.rate"},
    {"name": "service", "source": "line", "column": "service_per_min", "transform": "value",
     "target": f"blocks.{serve.name}.params.dist_params.rate"},
]


def wait_now():
    """Read the plant, write its values into the model, and run it."""
    bound, report = databind.resolve_bindings(layout, network=policy)
    values = {record.name: record.value for record in report.records}
    result = api.run_model(bound, replications=10, seed=1)
    return values, result.kpi("resource/Server/Time in Queue Avg")


values, wait = wait_now()
print(f"plant says {values}: mean wait {wait:.2f} minutes")

# 4. The plant changes: the service rate drops. Read again, and the forecast moves with it.
reading[0]["service_per_min"] = 1.1
values, wait = wait_now()
print(f"plant says {values}: mean wait {wait:.2f} minutes")
server.shutdown()
