"""A loopback-only HTTP interface. Simulation lifecycle stays with MTSManager."""

import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .install import ROOT, STATE, COMPONENTS, build_simulator, install_packages, run, write_entry


class Workspace:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.task = {"state": "idle", "title": "", "logs": [], "result": None}

    def log(self, line):
        with self.lock:
            self.task["logs"] = (self.task["logs"] + [str(line)])[-1500:]

    def start(self, title, function):
        with self.lock:
            if self.task["state"] == "running":
                raise ValueError("An operation is already running. Wait for it to finish.")
            self.task = {"state": "running", "title": title, "logs": [], "result": None}
        def work():
            try:
                result = function()
                with self.lock:
                    self.task.update(state="complete", result=result)
            except Exception as error:
                self.log(str(error))
                with self.lock:
                    self.task.update(state="failed", error=str(error))
        threading.Thread(target=work, daemon=True).start()

    def manager(self, *args):
        return run([sys.executable, "-m", "MTS_Management", "--sites",
                    str(ROOT / "MTSManager/sites.toml"), *map(str, args)], self.log)

    def status(self):
        modules = {"MTSManager": "MTS_Management", "MTSPlot": "MTS_Plotting", "Model math": "MTM_Math"}
        packages = {name: importlib.util.find_spec(module) is not None for name, module in modules.items()}
        site_file = ROOT / "MTSManager/sites.toml"
        sites = []
        local_build = False
        if packages["MTSManager"] and site_file.exists():
            from MTS_Management.cluster import load_sites, paths
            profiles = load_sites(site_file)
            sites = [{"name": s.name, "remote": bool(s.ssh), "mode": s.mode}
                     for s in profiles.values()]
            if "local" in profiles and not profiles["local"].ssh:
                executable = Path(paths(profiles["local"])["executable"])
                local_build = executable.is_file() and (executable.parent / "build-manifest.json").is_file()
        with self.lock:
            task = dict(self.task, logs=list(self.task["logs"]))
        return {"workspace": str(ROOT), "python": sys.version.split()[0],
                "components": {name: (ROOT / name / ("CMakeLists.txt" if name == "MTS2D"
                                                    else "pyproject.toml")).exists() for name in COMPONENTS},
                "packages": packages, "local_build": local_build,
                "data_folders": [str(p) for p in [ROOT / "MTSManager/output", *sorted(
                    (ROOT / "MTSPlot/data").glob("*"))] if p.is_dir()],
                "sites": sites, "task": task,
                "setup_command": f'bash "{ROOT}/setup.sh" --local-sim --install-build-tools'}

    def operation(self, action, data):
        if action == "install":
            return install_packages(self.log)
        if action == "build":
            return build_simulator(self.log)
        if action == "inventory":
            from MTS_Management.cluster import call, load_sites
            site = load_sites(ROOT / "MTSManager/sites.toml")[data.get("site", "local")]
            return call(site, "inventory", timeout=120)
        if action in {"logs", "stop"}:
            from MTS_Management.cluster import call, load_sites
            site = load_sites(ROOT / "MTSManager/sites.toml")[data.get("site", "local")]
            result = call(site, action, attempt=data["attempt"])
            if action == "logs":
                self.log(result)
            return result
        if action == "run":
            from MTS_Management.config import Config
            config = Config(**data["settings"])
            folder = STATE / "configurations"
            folder.mkdir(exist_ok=True)
            path = folder / (config.config_hash + ".conf")
            config.write(path, data.get("label") or None)
            command = ["run", path, "--site", data.get("site", "local"), "--campaign", "GUI"]
            if data.get("label"):
                command += ["--label", data["label"]]
            if data.get("site", "local") == "local":
                command += ["--mode", "detached"]
            self.manager(*command)
            return {"config_hash": config.config_hash}
        if action == "plot":
            from MTS_Plotting.benchmark import figure_name
            slots = {key: data[key] for key in ("y", "x", "color", "gradient", "fixed")}
            # Matplotlib runs in its own process, isolated from HTTP request threads.
            folder = STATE / "figures" / secrets.token_hex(6)
            self.log("Reading simulation tables and creating the figure…")
            command = [sys.executable, "-m", "MTS_Plotting", str(Path(data["data"]).expanduser().resolve()),
                       str(folder), "--metric", slots["y"], "--x", slots["x"], "--color", slots["color"],
                       "--fixed", json.dumps(slots["fixed"]), "--legend-ncols", str(data.get("ncols", 1)),
                       "--legend-loc", data.get("legend_loc", "best")]
            if slots["gradient"]:
                command += ["--gradient", slots["gradient"]]
            for limit in ("xlim", "ylim"):
                if data.get(limit) is not None:
                    command += ["--" + limit, *map(str, data[limit])]
            run(command, self.log)
            path = folder / figure_name(**slots)
            return {"pdf": str(path.relative_to(STATE / "figures")), "name": path.name}
        raise ValueError(f"Unknown operation: {action}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Request URLs can contain the local access token.

    def reply(self, status, value, content_type="application/json"):
        body = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; "
                         "script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        if self.headers.get("Host") != self.server.authority:
            self.reply(403, {"error": "This interface is available only at its local launch address."})
            return False
        path = urlsplit(self.path)
        if path.path.startswith("/api/") or path.path.startswith("/figures/"):
            token = self.headers.get("X-MTS-Token") or parse_qs(path.query).get("token", [""])[0]
            if not secrets.compare_digest(token, self.server.app.token):
                self.reply(403, {"error": "Open MTS using the desktop launcher to start a valid session."})
                return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/health":
                self.reply(200, {"application": "MTS", "workspace": str(ROOT), "pid": os.getpid()})
            elif path == "/api/state":
                self.reply(200, self.server.app.status())
            else:
                if path.startswith("/figures/"):
                    base = STATE / "figures"
                    file = (base / path.removeprefix("/figures/")).resolve()
                    if not file.is_relative_to(base) or file.suffix != ".pdf":
                        raise ValueError("Invalid figure path")
                elif path == "/InstallationGuide.html":
                    file = ROOT / "InstallationGuide.html"
                else:
                    base = Path(__file__).parent / "static"
                    asset = path.removeprefix("/MTS_GUI/static/") if path.startswith("/MTS_GUI/static/") else path.lstrip("/")
                    file = (base / ("index.html" if path == "/" else asset)).resolve()
                    if not file.is_relative_to(base):
                        raise ValueError("Invalid asset path")
                self.reply(200, file.read_bytes(), mimetypes.guess_type(file)[0] or "application/octet-stream")
        except FileNotFoundError:
            self.reply(404, {"error": "File not found"})
        except Exception as error:
            self.reply(400, {"error": str(error)})

    def do_POST(self):
        if not self.allowed():
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.server.authority:
            self.reply(403, {"error": "Requests must originate from this MTS session."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536 or self.headers.get("Content-Type") != "application/json":
                raise ValueError("Expected a JSON request of at most 64 KiB")
            data = json.loads(self.rfile.read(length))
            action = urlsplit(self.path).path.removeprefix("/api/")
            if action == "quit":
                if self.server.app.task["state"] == "running":
                    raise ValueError("Wait for the current operation to finish before quitting MTS.")
                self.reply(200, {"stopped": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif action in {"install", "build", "inventory", "logs", "stop", "run", "plot"}:
                self.server.app.start(action.capitalize(), lambda: self.server.app.operation(action, data))
                self.reply(202, {"started": action})
            else:
                self.reply(404, {"error": "Unknown operation"})
        except Exception as error:
            self.reply(400, {"error": str(error)})


def serve(install=False, local_sim=False):
    STATE.mkdir(exist_ok=True)
    app = Workspace()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.app = app
    server.authority = f"127.0.0.1:{server.server_port}"
    url = f"http://{server.authority}/#{app.token}"
    record = STATE / "server.json"
    temporary = STATE / "server.json.tmp"
    temporary.write_text(json.dumps({"url": url, "pid": os.getpid(), "workspace": str(ROOT)}))
    temporary.chmod(0o600)
    temporary.replace(record)
    write_entry(url)
    if install:
        def setup():
            install_packages(app.log)
            if local_sim:
                build_simulator(app.log)
        app.start("Set up MTS", setup)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        record.unlink(missing_ok=True)
        write_entry()
