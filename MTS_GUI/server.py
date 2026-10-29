"""A loopback-only HTTP interface. Simulation lifecycle stays with MTSManager."""

import importlib.util
from contextlib import closing
from concurrent.futures import CancelledError
from dataclasses import asdict
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .install import ROOT, STATE, COMPONENTS, build_simulator, install_packages, run, write_entry


def site_label(name):
    return "MAGI" + name[4:] if name == "magi" or name.startswith("magi-") else name


class Workspace:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.task = {"state": "idle", "title": "", "logs": [], "result": None}
        self.history = []
        self.stopping = False
        self.cancel = threading.Event()
        self.campaign_files = {}
        self.local_files = {}
        self.ssh_results = []
        self.ssh_proposals = {}
        self.ssh_addresses = {}
        self.ssh_config_stamp = None

    def log(self, line, *, command=False):
        with self.lock:
            if command or str(line).startswith("$ "):
                self.task["command"] = str(line).removeprefix("$ ")
            line = f"[{time.strftime('%H:%M:%S')}] {line}"
            self.task["logs"] = (self.task["logs"] + [line])[-1500:]
        print(line, flush=True)

    def start(self, title, function, *, cancellable=False):
        with self.lock:
            if self.stopping:
                raise ValueError("MTS is shutting down.")
            if self.task["state"] == "running":
                raise ValueError("An operation is already running. Wait for it to finish.")
            self.task = {"id": secrets.token_hex(8), "state": "running", "title": title,
                         "created_at": time.time() * 1000, "logs": [], "result": None,
                         "cancellable": cancellable}
            self.cancel.clear()
            self.history.append(self.task)
            task_id = self.task["id"]
        def work():
            started = time.monotonic()
            self.log(f"{title}: started")
            try:
                result = function()
                if isinstance(result, str):
                    self.log(result)
                partial = isinstance(result, dict) and bool(result.get("errors"))
                self.log(f"{title}: completed{' with errors' if partial else ''} in {time.monotonic() - started:.2f} s")
                with self.lock:
                    self.task.update(state="partial" if partial else "complete", result=result)
            except CancelledError as error:
                self.log(str(error))
                with self.lock:
                    self.task.update(state="aborted")
            except Exception as error:
                for stream in ("stdout", "stderr"):
                    output = getattr(error, stream, None)
                    if output:
                        self.log(f"{stream}: {output.decode(errors='replace') if isinstance(output, bytes) else output}")
                self.log(traceback.format_exc())
                self.log(f"{title}: failed after {time.monotonic() - started:.2f} s")
                with self.lock:
                    self.task.update(state="failed", error=str(error))
        threading.Thread(target=work, daemon=True).start()
        return task_id

    def check_cancelled(self):
        if self.cancel.is_set():
            raise CancelledError("Operation aborted. Unfinished updates retain their previous cached data.")

    def abort(self):
        with self.lock:
            if self.task["state"] != "running" or not self.task.get("cancellable"):
                raise ValueError("This operation cannot be safely interrupted.")
            self.cancel.set()
            self.task["cancel_requested"] = True
        self.log("Abort requested; stopping the current read and skipping remaining servers.")

    def manager(self, *args):
        return run([sys.executable, "-m", "MTS_Management", "--sites",
                    str(ROOT / "MTSManager/sites.toml"), *map(str, args)], self.log, json_result=True)

    def manager_call(self, site_name, action, *, site=None, **options):
        from MTS_Management.cluster import call, load_sites
        site_file = ROOT / "MTSManager/sites.toml"
        self.log(f"from MTS_Management.cluster import call, load_sites\n"
                 f"site = load_sites({str(site_file)!r})[{site_name!r}]")
        self.log(f"call(site, {action!r}" + "".join(f", {k}={v!r}" for k, v in options.items()) + ")",
                 command=True)
        site = site or load_sites(site_file)[site_name]
        self.log(f"Waiting for {site.ssh or 'local execution'}; directory={site.directory!r}, "
                 f"module={site.management_module!r}, output_root={site.output_root!r}")
        return call(site, action, log=self.log, cancel=self.cancel, **options)

    def inventory_cache(self, site_name, *, rows=None, site=None):
        """Read locally; replace a snapshot only after a successful inventory."""
        from MTS_Management.cluster import load_sites
        site = site or load_sites(ROOT / "MTSManager/sites.toml")[site_name]
        identity_settings = asdict(site)
        identity_settings.pop("manager_directory", None)
        identity_settings.pop("disabled", None)
        identity = hashlib.sha256(json.dumps(identity_settings, sort_keys=True).encode()).hexdigest()
        STATE.mkdir(exist_ok=True)
        with closing(sqlite3.connect(STATE / "inventory.sqlite3")) as database, database:
            database.execute("CREATE TABLE IF NOT EXISTS inventory "
                             "(identity TEXT PRIMARY KEY, updated_at REAL NOT NULL, rows TEXT NOT NULL)")
            if rows is not None:
                if not isinstance(rows, list):
                    raise ValueError("Inventory response must be a list of simulations")
                database.execute("INSERT OR REPLACE INTO inventory VALUES (?, ?, ?)",
                                 (identity, time.time(), json.dumps(rows)))
            record = database.execute("SELECT updated_at, rows FROM inventory WHERE identity = ?",
                                      (identity,)).fetchone()
        return {"site": site_name, "updated_at": record[0] if record else None,
                "rows": json.loads(record[1]) if record else []}

    def campaign_definitions(self):
        from MTS_Management.campaign import campaign_identity
        definitions = {}
        for path in sorted((ROOT / "MTSManager/campaigns").rglob("simulations.json")):
            stamp = path.stat().st_mtime_ns
            if path not in self.campaign_files or self.campaign_files[path][0] != stamp:
                name, expected = campaign_identity(path)
                self.campaign_files[path] = (stamp, name, expected)
            _, name, expected = self.campaign_files[path]
            if name in definitions and definitions[name] != expected:
                raise ValueError(f"Conflicting campaign definitions: {name}")
            definitions[name] = expected
        return definitions

    def local_copies(self):
        """Inspect managed download collections, without traversing simulation output trees."""
        from MTS_Management.config import Config
        from MTS_Management.files import file_digest, read_json
        from MTS_Management.inventory import progress, config_metadata_stamp
        rows = []
        for manifest in (ROOT / "MTSManager/.state/retrieved").glob("*/*/manifest.json"):
            folder = manifest.parent
            paths = [manifest, folder / "config.conf", folder / "macroData.csv", folder / "termination.json"]
            stamp = tuple(p.stat().st_mtime_ns if p.exists() else None for p in paths) + (tuple(tuple(v) for v in config_metadata_stamp()),)
            if manifest not in self.local_files or self.local_files[manifest][0] != stamp:
                record = read_json(manifest)
                config = Config.read(folder / "config.conf", expected_name=folder.name)
                if config.config_hash != record["config_hash"]:
                    raise ValueError(f"Local manifest/config mismatch: {folder}")
                termination = read_json(paths[-1])["termination"] if paths[-1].exists() else None
                request = next(folder.glob("attempts/*/request.json"), None)
                request_data = read_json(request) if request else {}
                origin = request_data.get("site")
                row = {"config_hash": config.config_hash, "folder": str(folder), "attempt": None,
                       "campaign": record.get("campaign"), "manifest_sha256": file_digest(manifest),
                       "status": {"state": "finished" if termination else "local copy"},
                       "termination": termination, "progress": progress(folder, config),
                       "site": None, "origin_site": origin, "local_copy": True,
                       "origin_state_dir": request_data.get("state_dir", ""),
                       "local_updated_at": max(v for v in stamp[:-1] if v is not None) / 1e9}
                self.local_files[manifest] = (stamp, row)
            rows.append(dict(self.local_files[manifest][1]))
        return rows

    def inventory_overview(self, *, site=None, campaign=None):
        from MTS_Management.cluster import load_sites
        if site is not None and campaign is not None:
            raise ValueError("Select either a server or a campaign")
        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        snapshots = {name: self.inventory_cache(name, site=profile) for name, profile in profiles.items()}
        definitions, copies = self.campaign_definitions(), self.local_copies()
        for row in copies:
            origin = profiles.get(row["origin_site"])
            candidates = [name for name, profile in profiles.items() if origin is not None and
                          profile.ssh == origin.ssh and row["origin_state_dir"].endswith(
                              "/" + str(Path(profile.directory.removeprefix("~/")) / profile.state_dir).lstrip("/"))]
            row["origin_profile"] = candidates[0] if len(candidates) == 1 else None
        expected = definitions.get(campaign, {})
        matches = lambda row: row.get("campaign") == campaign or row["config_hash"] in expected
        campaigns = sorted(set(definitions) | {row["campaign"] for row in copies + [
            row for snapshot in snapshots.values() for row in snapshot["rows"]] if row.get("campaign")})
        selected = ({name: snapshot for name, snapshot in snapshots.items()
                     if profiles[name].ssh == profiles[site].ssh} if site is not None else
                    {name: snapshot for name, snapshot in snapshots.items()
                     if any(matches(row) for row in snapshot["rows"]) or
                     any(matches(row) and row["origin_profile"] == name for row in copies)})
        rows = [row | {"site": name} for name, snapshot in selected.items() for row in snapshot["rows"]
                if campaign is None or matches(row)]
        for row in copies:
            origin = profiles.get(row["origin_site"])
            if site is not None:
                relevant = not profiles[site].ssh or (origin is not None and origin.ssh == profiles[site].ssh)
            else:
                relevant = matches(row)
            if not relevant:
                continue
            remote = next((r for r in rows if r["config_hash"] == row["config_hash"] and
                           r.get("manifest_sha256") == row["manifest_sha256"]), None)
            if remote is not None:
                remote.setdefault("local_paths", []).append(row["folder"])
            else:
                rows.append(row)
        found = {row["config_hash"] for row in rows}
        missing = set(expected) - found
        rows.extend({"config_hash": h, "folder": expected[h], "site": None, "attempt": None,
                     "campaign": campaign, "status": {"state": "missing"}, "termination": None,
                     "progress": {"fraction": None}, "missing": True} for h in sorted(missing))
        sources = [{"site": name, "label": site_label(name), "updated_at": snapshot["updated_at"]}
                   for name, snapshot in selected.items()]
        updated = [source["updated_at"] for source in sources if source["updated_at"] is not None]
        return {"site": site, "campaign": campaign, "rows": rows, "sources": sources,
                "updated_at": min(updated) if updated else None, "campaigns": campaigns,
                "expected": len(expected) if campaign in definitions else None,
                "found": len(set(expected) & found), "missing": len(missing),
                "uncached_sites": [site_label(name) for name, snapshot in snapshots.items()
                                   if snapshot["updated_at"] is None]}

    def clear_terminal(self, entry_id=None):
        with self.lock:
            self.history = [entry for entry in self.history if entry_id and entry["id"] != entry_id]

    def status(self):
        modules = {"MTSManager": "MTS_Management", "MTSPlot": "MTS_Plotting", "Model math": "MTM_Math"}
        packages = {name: importlib.util.find_spec(module) is not None for name, module in modules.items()}
        site_file = ROOT / "MTSManager/sites.toml"
        sites = []
        servers = []
        local_build = False
        if packages["MTSManager"] and site_file.exists():
            from MTS_Management.cluster import load_sites, paths
            profiles = load_sites(site_file)
            sites = [{"name": s.name, "label": site_label(s.name), "remote": bool(s.ssh), "mode": s.mode}
                     for s in profiles.values()]
            seen = set()
            for profile in profiles.values():
                host = profile.ssh or "local"
                if host not in seen:
                    servers.append({"name": profile.name, "label": site_label(host),
                                    "profiles": [p.name for p in profiles.values() if (p.ssh or "local") == host]})
                    seen.add(host)
            if "local" in profiles and not profiles["local"].ssh:
                executable = Path(paths(profiles["local"])["executable"])
                local_build = executable.is_file() and (executable.parent / "build-manifest.json").is_file()
        with self.lock:
            task = dict(self.task, logs=list(self.task["logs"]))
            history = [{k: list(v) if k == "logs" else v for k, v in entry.items() if k != "result"}
                       for entry in self.history]
        return {"workspace": str(ROOT), "python": sys.version.split()[0],
                "components": {name: (ROOT / name / ("CMakeLists.txt" if name == "MTS2D"
                                                    else "pyproject.toml")).exists() for name in COMPONENTS},
                "packages": packages, "local_build": local_build,
                "data_folders": [str(p) for p in [ROOT / "MTSManager/output", *sorted(
                    (ROOT / "MTSPlot/data").glob("*"))] if p.is_dir()],
                "sites": sites, "servers": servers, "task": task, "history": history, "interface_version": 3,
                "ssh_connections": self.connection_status(site_file) if packages["MTSManager"] and site_file.exists() else [],
                "setup_command": f'bash "{ROOT}/setup.sh" --local-sim --install-build-tools'}

    def connection_status(self, site_file):
        from .ssh_connections import connections, effective
        config = Path.home() / ".ssh/config"
        stamp = config.stat().st_mtime_ns if config.exists() else 0
        if stamp != self.ssh_config_stamp:
            self.ssh_addresses = {}
            self.ssh_config_stamp = stamp
        checked = {row["host"]: row for row in self.ssh_results}
        result = []
        for row in connections(site_file):
            host = row["host"]
            if host not in self.ssh_addresses:
                try:
                    settings = effective(host)
                    self.ssh_addresses[host] = f"{settings['user']}@{settings['hostname']}:{settings['port']}"
                except (OSError, subprocess.SubprocessError):
                    self.ssh_addresses[host] = host
            entry = checked.get(host, {}) | row | {"address": self.ssh_addresses[host]}
            if row["disabled"]:
                entry.update(status="Disabled", repair=False)
            elif checked.get(host, {}).get("disabled"):
                entry.update(status="Unchecked", repair=False)
            result.append(entry)
        return result

    def operation(self, action, data):
        safe_data = {k: "[redacted]" if k == "password" else v for k, v in data.items()}
        self.log(f"Workspace.operation({action!r}, {safe_data!r})", command=True)
        if action.startswith("ssh_"):
            from . import ssh_connections as ssh
            site_file = ROOT / "MTSManager/sites.toml"
            host = data.get("host")
            if action == "ssh_check":
                result = ssh.check_connections(site_file, self.log, self.ssh_proposals)
                self.ssh_results = result["connections"]
                return result
            if action == "ssh_add":
                return ssh.add_connection(site_file, data)
            rows = {row["host"]: row for row in ssh.connections(site_file)}
            if host not in rows:
                raise ValueError("Unknown SSH connection")
            if action == "ssh_toggle":
                result = ssh.set_disabled(site_file, host, data["disabled"])
                self.ssh_results = [row for row in self.ssh_results if row["host"] != host]
                self.ssh_proposals.pop(host, None)
                return result
            if action == "ssh_configure":
                result = ssh.configure_address(host, data)
                self.ssh_results = [row for row in self.ssh_results if row["host"] != host]
                self.ssh_proposals.pop(host, None)
                return result
            if action == "ssh_profile":
                result = ssh.save_profile(site_file, host, data)
                self.ssh_results = [row for row in self.ssh_results if row["host"] != host]
                return result
            if rows[host]["disabled"]:
                raise ValueError("Enable this connection before installing keys or accepting a host key")
            if action == "ssh_inspect":
                return ssh.inspect_server(host, self.log, setup=data.get("setup", ""))
            if action == "ssh_deploy":
                from MTS_Management.deployments import update_managers
                result = update_managers(site_file, host=host, log=self.log)
                self.ssh_results = [row for row in self.ssh_results if row["host"] != host]
                return result
            if action == "ssh_scan":
                proposal = ssh.scan_key(host)
                self.ssh_proposals[host] = proposal
                self.ssh_results = [row | {"repair": True, "fingerprint": proposal["fingerprint"],
                    "suggestion": "Accept the host key to update the saved connection identity."}
                    if row["host"] == host else row for row in self.ssh_results]
                return {"host": host, "fingerprint": proposal["fingerprint"]}
            if action == "ssh_accept":
                proposal = self.ssh_proposals.get(host)
                if proposal is None or data.get("fingerprint") != proposal["fingerprint"]:
                    raise ValueError("Check connections and review the current fingerprint first")
                result = ssh.accept_key(host, proposal)
                self.ssh_proposals.pop(host, None)
                self.ssh_results = [row for row in self.ssh_results if row["host"] != host]
                self.log(f"{host}: accepted host key {result['fingerprint']}")
                return result
            if action == "ssh_install":
                result = ssh.install_key(host, data["password"], self.log)
                self.ssh_results = [row for row in self.ssh_results if row["host"] != host]
                return result
            raise ValueError("Unknown SSH operation")
        if action == "install":
            return install_packages(self.log)
        if action == "build":
            return build_simulator(self.log)
        if action == "update_managers":
            from MTS_Management.deployments import update_managers
            self.log("MTS_Management.deployments.update_managers(sites.toml)", command=True)
            return update_managers(ROOT / "MTSManager/sites.toml", log=self.log)
        if action in {"inventory", "hard_refresh"}:
            from MTS_Management.cluster import load_sites
            profiles = load_sites(ROOT / "MTSManager/sites.toml")
            campaign = data.get("campaign")
            selection = {"campaign": campaign} if campaign is not None else {"site": data.get("site", "local")}
            names = (list(profiles) if data.get("site") == "*" else
                     [s["site"] for s in self.inventory_overview(**selection)["sources"]])
            if not names:
                raise ValueError("No cached servers for this campaign. Refresh the relevant server inventories first.")
            errors, updated = {}, []
            for index, name in enumerate(names, 1):
                self.check_cancelled()
                self.log(f"Updating {site_label(name)} ({index}/{len(names)})")
                try:
                    options = {"hard_refresh": True} if action == "hard_refresh" else {}
                    rows = self.manager_call(name, "inventory", site=profiles[name], timeout=120, **options)
                    self.check_cancelled()
                    self.inventory_cache(name, rows=rows, site=profiles[name])
                    updated.append(name)
                    with self.lock:
                        self.task["inventory_revision"] = len(updated)
                    self.log(f"Inventory updated: {site_label(name)}; {len(rows)} simulations saved locally.")
                except CancelledError:
                    raise
                except Exception as error:
                    if campaign is None and data.get("site") != "*":
                        raise
                    errors[name] = str(error)
                    detail = getattr(error, "stderr", None)
                    self.log(f"{site_label(name)} failed: {error}" + (f"\n{detail}" if detail else ""))
            if not updated:
                raise ValueError("No server inventories were updated. Previous snapshots retained.")
            return {"inventory_updated": True, "updated_sites": updated, "errors": errors}
        if action in {"logs", "stop"}:
            return self.manager_call(data.get("site", "local"), action, attempt=data["attempt"])
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
                self.reply(200, {"application": "MTS", "workspace": str(ROOT), "pid": os.getpid(),
                                 "restarting": getattr(self.server, "restart_requested", False)})
            elif path == "/api/state":
                self.reply(200, self.server.app.status())
            elif path == "/api/ssh_settings":
                from .ssh_connections import connections, editor_settings
                host = parse_qs(urlsplit(self.path).query).get("host", [""])[0]
                site_file = ROOT / "MTSManager/sites.toml"
                if host not in {row["host"] for row in connections(site_file)}:
                    raise ValueError("Unknown SSH connection")
                self.reply(200, editor_settings(site_file, host))
            elif path == "/api/inventory":
                query = parse_qs(urlsplit(self.path).query)
                campaign = query.get("campaign", [None])[0]
                site = query.get("site", [None if campaign is not None else "local"])[0]
                self.reply(200, self.server.app.inventory_overview(site=site, campaign=campaign))
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
            if action == "terminal":
                self.server.app.clear_terminal(data.get("id"))
                self.reply(200, {"cleared": True})
            elif action == "abort":
                self.server.app.abort()
                self.reply(200, {"requested": True})
            elif action in {"quit", "restart"}:
                with self.server.app.lock:
                    if self.server.app.task["state"] == "running":
                        raise ValueError("Wait for the current operation to finish before quitting or restarting MTS.")
                    if self.server.app.stopping:
                        raise ValueError("MTS is already shutting down.")
                    self.server.app.stopping = True
                    self.server.restart_requested = action == "restart"
                self.reply(200, {"restarting": self.server.restart_requested})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif action in {"install", "build", "inventory", "hard_refresh", "update_managers", "logs", "stop", "run", "plot",
                            "ssh_check", "ssh_add", "ssh_toggle", "ssh_accept", "ssh_install", "ssh_scan", "ssh_configure", "ssh_profile", "ssh_inspect", "ssh_deploy"}:
                task_id = self.server.app.start(action.capitalize(), lambda: self.server.app.operation(action, data),
                                                cancellable=action in {"inventory", "hard_refresh", "logs"})
                self.reply(202, {"started": action, "task_id": task_id})
            else:
                self.reply(404, {"error": "Unknown operation"})
        except Exception as error:
            self.reply(400, {"error": str(error)})


def serve(install=False, local_sim=False, restart=False):
    STATE.mkdir(exist_ok=True)
    app = Workspace()
    record = STATE / "server.json"
    port = 0
    if restart:
        saved = json.loads(record.read_text())
        parts = urlsplit(saved["url"])
        if (saved["pid"] != os.getpid() or saved["workspace"] != str(ROOT) or
                parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port or not parts.fragment):
            raise RuntimeError("Invalid saved restart session. Reopen MTS with the desktop launcher.")
        port, app.token = parts.port, parts.fragment
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    server.restart_requested = False
    server.authority = f"127.0.0.1:{server.server_port}"
    url = f"http://{server.authority}/#{app.token}"
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
        if not server.restart_requested:
            record.unlink(missing_ok=True)
            write_entry()
    return server.restart_requested
