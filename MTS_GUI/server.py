"""A loopback-only HTTP interface. Simulation lifecycle stays with MTSManager."""

import importlib.util
from contextlib import closing
from concurrent.futures import CancelledError, ProcessPoolExecutor, TimeoutError as FutureTimeout
import multiprocessing
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
    return str(name).upper()


def seed_group(settings):
    from MTS_Management.files import digest
    return {"settings_group": digest({k: v for k, v in settings.items() if k != "seed"}),
            "seed": settings["seed"]}


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
        self.preview_pool = None
        self.preview_lock = threading.Lock()
        self.preview_future = None
        self.preview_key = None
        self.preview_image = None
        self.slurm_plan = None

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
        arguments = "".join(f", {k}=" + (f"<{len(v)} saved configuration hashes>" if k == "config_hashes" else repr(v))
                            for k, v in options.items())
        self.log(f"call(site, {action!r}{arguments})", command=True)
        site = site or load_sites(site_file)[site_name]
        self.log(f"Waiting for {site.ssh or 'local execution'}; directory={site.directory!r}, "
                 f"module={site.management_module!r}, output_root={site.output_root!r}")
        return call(site, action, log=self.log, cancel=self.cancel, **options)

    def inventory_cache(self, site_name, *, rows=None, site=None, config_hashes=None):
        """Merge scoped results atomically, retaining unrelated rows and refresh times."""
        from MTS_Management.cluster import load_sites
        site = site or load_sites(ROOT / "MTSManager/sites.toml")[site_name]
        identity_settings = asdict(site)
        identity_settings.pop("manager_directory", None)
        identity_settings.pop("disabled", None)
        identity = hashlib.sha256(json.dumps(identity_settings, sort_keys=True).encode()).hexdigest()
        requested = set(config_hashes) if config_hashes is not None else None
        scope = hashlib.sha256(json.dumps(sorted(requested)).encode()).hexdigest() if requested is not None else "*"
        STATE.mkdir(exist_ok=True)
        with closing(sqlite3.connect(STATE / "inventory.sqlite3")) as database, database:
            database.execute("CREATE TABLE IF NOT EXISTS inventory "
                             "(identity TEXT PRIMARY KEY, updated_at REAL NOT NULL, rows TEXT NOT NULL)")
            database.execute("CREATE TABLE IF NOT EXISTS inventory_refresh "
                             "(identity TEXT, scope TEXT, updated_at REAL, PRIMARY KEY(identity, scope))")
            record = database.execute("SELECT updated_at, rows FROM inventory WHERE identity = ?", (identity,)).fetchone()
            if rows is not None:
                if not isinstance(rows, list):
                    raise ValueError("Inventory response must be a list of simulations")
                if requested is not None:
                    if any(row["config_hash"] not in requested for row in rows):
                        raise ValueError("Scoped inventory returned an unrelated configuration")
                    database.execute("INSERT OR IGNORE INTO inventory_refresh VALUES (?, '*', ?)",
                                     (identity, record[0] if record else None))
                    previous = json.loads(record[1]) if record else []
                    rows = [row for row in previous if row["config_hash"] not in requested] + rows
                else:
                    database.execute("DELETE FROM inventory_refresh WHERE identity = ?", (identity,))
                updated_at = time.time()
                database.execute("INSERT OR REPLACE INTO inventory VALUES (?, ?, ?)",
                                 (identity, updated_at, json.dumps(rows)))
                database.execute("INSERT OR REPLACE INTO inventory_refresh VALUES (?, ?, ?)",
                                 (identity, scope, updated_at))
            record = database.execute("SELECT updated_at, rows FROM inventory WHERE identity = ?",
                                      (identity,)).fetchone()
            refreshed = dict(database.execute("SELECT scope, updated_at FROM inventory_refresh WHERE identity = ?", (identity,)))
        return {"site": site_name, "updated_at": refreshed.get(scope, refreshed.get("*", record[0] if record else None)),
                "rows": json.loads(record[1]) if record else []}

    def campaign_definitions(self):
        from MTS_Management.campaign import campaign_identity
        definitions = {}
        for path in sorted((ROOT / "MTSManager/campaigns").rglob("simulations.json")):
            stamp = path.stat().st_mtime_ns
            if path not in self.campaign_files or self.campaign_files[path][0] != stamp:
                name, expected = campaign_identity(path)
                runs = json.loads(path.read_text())["runs"]
                groups = {h: seed_group(run["settings"]) for h, run in zip(expected, runs)}
                self.campaign_files[path] = (stamp, name, expected, groups)
            _, name, expected, _ = self.campaign_files[path]
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
                       **seed_group(config.as_dict()),
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
        config_hashes = sorted(set(expected) | {row["config_hash"] for row in copies + [
            row for snapshot in snapshots.values() for row in snapshot["rows"]] if row.get("campaign") == campaign}) if campaign is not None else None
        if campaign is not None:
            snapshots = {name: self.inventory_cache(name, site=profile, config_hashes=config_hashes)
                         for name, profile in profiles.items()}
        selected = ({name: snapshot for name, snapshot in snapshots.items()
                     if profiles[name].ssh == profiles[site].ssh} if site is not None else
                    snapshots)
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
        groups = {h: group for _, _, _, saved in self.campaign_files.values() for h, group in saved.items()}
        for row in rows:
            if row["config_hash"] not in groups and not row.get("settings_group"):
                path = STATE / "configurations" / (row["config_hash"] + ".conf")
                if not path.exists() and row.get("site") and not profiles[row["site"]].ssh:
                    path = Path(row["folder"]) / "config.conf"
                if path.exists():
                    from MTS_Management.config import Config
                    config = Config.read(path)
                    if config.config_hash != row["config_hash"]:
                        raise ValueError(f"Saved configuration hash mismatch: {path}")
                    groups[row["config_hash"]] = seed_group(config.as_dict())
            row.update(groups.get(row["config_hash"], {}))
        sources = [{"site": name, "label": site_label(name), "updated_at": snapshot["updated_at"]}
                   for name, snapshot in selected.items()]
        updated = [source["updated_at"] for source in sources if source["updated_at"] is not None]
        return {"site": site, "campaign": campaign, "rows": rows, "sources": sources,
                "updated_at": min(updated) if updated else None, "campaigns": campaigns,
                "config_hashes": config_hashes,
                "expected": len(expected) if campaign in definitions else None,
                "found": len(set(expected) & found), "missing": len(missing),
                "uncached_sites": [site_label(name) for name, snapshot in snapshots.items()
                                   if snapshot["updated_at"] is None]}

    def clear_terminal(self, entry_id=None):
        with self.lock:
            self.history = [entry for entry in self.history
                            if (entry["id"] != entry_id if entry_id else entry["state"] == "running")]

    def campaign_path(self, name):
        if name not in self.campaign_definitions():
            raise ValueError("Select a registered campaign.")
        return next(path for path, (_, campaign, _, _) in self.campaign_files.items() if campaign == name)

    def plot_options(self, campaign, data=None):
        from MTS_Management.cluster import load_sites, paths
        from MTS_Plotting.campaign_plot import describe
        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        sources = [paths(profile)["output_root"] for profile in profiles.values() if not profile.ssh]
        sources += list((ROOT / "MTSPlot/data").glob("*"))
        sources += list((ROOT / "MTSManager/.state/retrieved").glob("*"))
        return describe(self.campaign_path(campaign), sources, data)

    def slurm_options(self):
        from MTS_Management.cluster import load_sites
        from MTS_Management.slurm_control import fields, ACTIONS
        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        return {"fields": fields(), "actions": ACTIONS,
                "sites": [s.name for s in profiles.values() if s.mode == "slurm"]}

    def slurm_program(self, data):
        import shlex
        from MTS_Management.slurm_control import validate_rule
        rule = validate_rule({k: data[k] for k in ("action", "match", "criteria")})
        if data["site"] != "*" and data["site"] not in self.slurm_options()["sites"]:
            raise ValueError("Choose an enabled Slurm site.")
        command = [sys.executable, "-m", "MTS_Management.slurm_control", "--sites", str(ROOT / "MTSManager/sites.toml"),
                   "--site", data["site"], "--action", rule["action"], "--match", rule["match"],
                   "--criteria", json.dumps(rule["criteria"])]
        return {"review": shlex.join(command), "apply": shlex.join(command + ["--apply"])}

    def plot_presets(self, data=None):
        from MTS_Management.files import locked, read_json, write_json, now
        path = STATE / "plot-presets.json"
        with locked(STATE / ".plot-presets.lock"):
            saved = read_json(path) if path.exists() else {"version": 1, "presets": []}
            if saved.keys() != {"version", "presets"} or saved["version"] != 1 or not isinstance(saved["presets"], list):
                raise ValueError("Invalid saved plot presets.")
            if data is not None:
                name, selection = data["name"].strip(), data["selection"]
                if not name or len(name) > 120 or not isinstance(selection, dict):
                    raise ValueError("A preset requires a name of 1–120 characters and plot selections.")
                self.campaign_path(selection["campaign"])
                previous = next((p for p in saved["presets"] if p["id"] == data.get("id")), None)
                if data.get("id") and previous is None:
                    raise ValueError("The preset to update no longer exists.")
                if any(p["name"] == name and p is not previous for p in saved["presets"]):
                    raise ValueError("A preset with this name already exists. Choose another name or update that preset.")
                record = {"id": previous["id"] if previous else secrets.token_hex(8), "name": name,
                          "selection": selection, "updated_at": now()}
                if previous is None:
                    saved["presets"].append(record)
                else:
                    saved["presets"][saved["presets"].index(previous)] = record
                write_json(path, saved)
            return {"presets": sorted(saved["presets"], key=lambda p: p["name"].casefold()),
                    "saved_id": record["id"] if data is not None else None}

    def plot_preview(self, data):
        from MTS_Plotting.campaign_plot import preview
        selection = {k: v for k, v in data.items() if k != "automatic"}
        key = json.dumps(selection, sort_keys=True)
        with self.preview_lock:
            if self.preview_pool is None:
                self.preview_pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            if self.preview_future is not None and not self.preview_future.done() and key != self.preview_key:
                return {"busy": True}
            if key != self.preview_key or self.preview_future is None or self.preview_future.done():
                directory = STATE / "previews"
                directory.mkdir(exist_ok=True)
                path = directory / (secrets.token_hex(8) + ".png")
                self.preview_future = self.preview_pool.submit(preview, str(self.campaign_path(data["campaign"])),
                    data["data"], str(path), selection)
                self.preview_key = key
                self.preview_image = path.name
            future = self.preview_future
            image = self.preview_image
        try:
            result = future.result(timeout=5 if data.get("automatic", True) else None)
        except FutureTimeout:
            return {"manual": True}
        return result | {"image": image}

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
                "campaigns": sorted(self.campaign_definitions()) if packages["MTSManager"] else [],
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
            if row["disabled"] and host not in checked:
                entry.update(status="Disabled", repair=False)
            elif not row["disabled"] and checked.get(host, {}).get("status") == "Disabled":
                entry.update(status="Unchecked", repair=False)
            result.append(entry)
        return result

    def check_connections(self, host=None):
        from . import ssh_connections as ssh
        result = ssh.check_connections(ROOT / "MTSManager/sites.toml", self.log, self.ssh_proposals, host=host)
        checked = {row["host"] for row in result["connections"]}
        self.ssh_results = [row for row in self.ssh_results if row["host"] not in checked] + result["connections"]
        return result

    def update_servers(self, host=None):
        from MTS_Management.deployments import update_managers
        result = update_managers(ROOT / "MTSManager/sites.toml", host=host, log=self.log)
        self.check_connections(host)
        return result

    def operation(self, action, data):
        safe_data = {k: "[redacted]" if k == "password" else v for k, v in data.items()}
        self.log(f"Workspace.operation({action!r}, {safe_data!r})", command=True)
        if action.startswith("ssh_"):
            from . import ssh_connections as ssh
            site_file = ROOT / "MTSManager/sites.toml"
            host = data.get("host")
            if action == "ssh_check":
                return self.check_connections(host)
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
                checked = next((row for row in self.ssh_results if row["host"] == host), {})
                if checked.get("status") != "Connected":
                    raise ValueError("Check this connection before deployment")
                if checked.get("deployment", {}).get("state") not in {"missing", "mismatch"}:
                    raise ValueError("Deployment is current or has not been verified")
                return self.update_servers(host)
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
        if action == "slurm_plan":
            from MTS_Management.slurm_control import run_sites
            program = self.slurm_program(data)
            self.log(program["review"], command=True)
            rule = {k: data[k] for k in ("action", "match", "criteria")}
            results = run_sites(ROOT / "MTSManager/sites.toml", data["site"], rule, log=self.log)
            token = secrets.token_hex(12)
            self.slurm_plan = {"token": token, "created": time.monotonic(), "rule": rule, "site": data["site"],
                "reviewed": {name: [job["job_id"] for job in response["data"]["jobs"]]
                             for name, response in results.items() if "data" in response and response["data"]["jobs"]}}
            return {"slurm_plan": token, "sites": results, "errors": {n: r["error"] for n, r in results.items() if "error" in r}}
        if action == "slurm_execute":
            from MTS_Management.slurm_control import run_sites
            plan = self.slurm_plan
            if plan is None or data["token"] != plan["token"] or time.monotonic() - plan["created"] > 900:
                raise ValueError("Review the current selection before running it; plans expire after 15 minutes.")
            self.slurm_plan = None
            results = run_sites(ROOT / "MTSManager/sites.toml", plan["site"], plan["rule"],
                                apply=True, reviewed=plan["reviewed"], log=self.log)
            return {"slurm_result": True, "sites": results,
                    "errors": {n: r.get("error", "Some requested changes could not be verified") for n, r in results.items()
                               if "error" in r or r.get("data", {}).get("unverified")}}
        if action == "update_managers":
            self.log("MTS_Management.deployments.update_managers(sites.toml)", command=True)
            return self.update_servers()
        if action in {"inventory", "hard_refresh"}:
            from MTS_Management.cluster import load_sites
            profiles = load_sites(ROOT / "MTSManager/sites.toml")
            campaign = data.get("campaign")
            selection = {"campaign": campaign} if campaign is not None else {"site": data.get("site", "local")}
            overview = self.inventory_overview(**selection) if data.get("site") != "*" else None
            names = (list(profiles) if data.get("site") == "*" else
                     [s["site"] for s in overview["sources"]])
            if not names:
                raise ValueError("No enabled site profiles are available for inventory refresh.")
            config_hashes = overview["config_hashes"] if campaign is not None else None
            if campaign is not None and not config_hashes:
                raise ValueError("No configuration identities are recorded for this campaign. Register its simulations.json first.")
            errors, updated = {}, []
            for index, name in enumerate(names, 1):
                self.check_cancelled()
                self.log(f"Updating {site_label(name)} ({index}/{len(names)})")
                try:
                    options = {"hard_refresh": True} if action == "hard_refresh" else {}
                    if config_hashes is not None:
                        options["config_hashes"] = config_hashes
                    for attempt in range(1, 4):
                        self.check_cancelled()
                        self.log(f"Inventory attempt {attempt}/3; timeout 60 s")
                        try:
                            rows = self.manager_call(name, "inventory", site=profiles[name], timeout=60, **options)
                            break
                        except (subprocess.TimeoutExpired, TimeoutError, ConnectionError) as error:
                            self.check_cancelled()
                            if attempt == 3:
                                raise
                            self.log(f"{site_label(name)}: {error}; retrying.")
                    self.check_cancelled()
                    self.inventory_cache(name, rows=rows, site=profiles[name], config_hashes=config_hashes)
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
            # Matplotlib runs in its own process, isolated from HTTP request threads.
            folder = STATE / "figures" / secrets.token_hex(6)
            path = folder / "campaign-plot.pdf"
            self.log("Reading simulation tables and creating the figure…")
            options = {k: v for k, v in data.items() if k not in {"campaign", "data"}}
            result = run([sys.executable, "-m", "MTS_Plotting.campaign_plot", str(self.campaign_path(data["campaign"])),
                          str(Path(data["data"]).expanduser().resolve()), str(path), json.dumps(options)],
                         self.log, json_result=True)
            for warning in result["warnings"]:
                self.log(warning)
            return result | {"pdf": str(path.relative_to(STATE / "figures")), "name": path.name}
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
        if unquote(path.path).startswith(("/api/", "/figures/", "/previews/")):
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
            elif path == "/api/plot_options":
                query = parse_qs(urlsplit(self.path).query)
                self.reply(200, self.server.app.plot_options(query.get("campaign", [""])[0], query.get("data", [None])[0]))
            elif path == "/api/slurm_options":
                self.reply(200, self.server.app.slurm_options())
            elif path == "/api/plot_check":
                from MTS_Plotting.campaign_plot import prepared_plot, validate_axes
                data = json.loads(parse_qs(urlsplit(self.path).query)["selection"][0])
                _, result = prepared_plot(self.server.app.campaign_path(data["campaign"]), data["data"], data)
                result = result | {"warnings": result["warnings"] + validate_axes(result, data)}
                self.reply(200, {k: result[k] for k in ("warnings", "runs", "expected", "mode")})
            elif path == "/api/plot_presets":
                self.reply(200, self.server.app.plot_presets())
            else:
                if path.startswith("/previews/"):
                    base = (STATE / "previews").resolve()
                    file = (base / path.removeprefix("/previews/")).resolve()
                    if not file.is_relative_to(base) or file.suffix != ".png":
                        raise ValueError("Invalid preview path.")
                    self.reply(200, file.read_bytes(), "image/png")
                    return
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
            if action == "slurm_program":
                self.reply(200, self.server.app.slurm_program(data))
            elif action == "plot_preview":
                self.reply(200, self.server.app.plot_preview(data))
            elif action == "plot_presets":
                self.reply(200, self.server.app.plot_presets(data))
            elif action == "terminal":
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
            elif action in {"install", "build", "inventory", "hard_refresh", "update_managers", "logs", "stop", "run", "plot", "slurm_plan", "slurm_execute",
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
        if app.preview_pool is not None:
            app.preview_pool.shutdown(wait=False, cancel_futures=True)
        server.server_close()
        if not server.restart_requested:
            record.unlink(missing_ok=True)
            write_entry()
    return server.restart_requested
