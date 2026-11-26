"""A loopback-only HTTP interface. Simulation lifecycle stays with MTSManager."""

import importlib.util
from concurrent.futures import CancelledError
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .install import ROOT, STATE, COMPONENTS, build_simulator, install_packages, write_entry
from . import simulation_setup
from .monitor import Monitor, site_label
from .plots import Plots


def local_build_status(site_paths):
    from MTS_Management import provenance
    from MTS_Management.files import read_json

    executable = Path(site_paths["executable"])
    manifest = executable.parent / "build-manifest.json"
    if not executable.is_file() or not manifest.is_file():
        return "missing", False
    try:
        record = read_json(manifest)
    except (OSError, ValueError):
        return "missing", False
    if not isinstance(record, dict) or not record.get("revision"):
        return "missing", False

    source = Path(site_paths["source"])
    try:
        if (source / ".git").exists():
            revision = provenance.command(
                ["git", "-C", str(source), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        else:
            revision = read_json(source / "SOURCE.json")["revision"]
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError):
        revision = None
    supports_current = getattr(provenance, "supports_current_configuration", None)
    current = bool(supports_current and supports_current(record))
    outdated = (revision and record["revision"] != revision) or not current
    return ("outdated" if outdated else "installed"), current


class Workspace:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.task = {"state": "idle", "title": "", "logs": [], "result": None}
        self.progress_lines = {}
        self.history = []
        self.stopping = False
        self.monitor = Monitor(self, ROOT, STATE)
        self.plots = Plots(self, ROOT, STATE)
        self.cancel = threading.Event()
        self.ssh_results = []
        self.ssh_proposals = {}
        self.ssh_addresses = {}
        self.ssh_config_stamp = None
        self.slurm_plan = None

    def log(self, line, *, command=False, progress=None):
        with self.lock:
            if command or str(line).startswith("$ "):
                self.task["command"] = str(line).removeprefix("$ ")
            line = f"[{time.strftime('%H:%M:%S')}] {line}"
            logs = self.task["logs"]
            if progress is not None and progress in self.progress_lines:
                logs[self.progress_lines[progress]] = line
            else:
                if progress is not None:
                    self.progress_lines[progress] = len(logs)
                logs.append(line)
                if len(logs) > 1500:
                    del logs[0]
                    self.progress_lines = {key: index - 1 for key, index in self.progress_lines.items() if index}
        print(line, flush=True)

    def log_build(self, line, *, host="local"):
        compiling = re.match(r"^\[\s*(?:\d+%|\d+/\d+)\]\s+(?:Building|Linking|Built target|Generating)\b", line)
        if compiling and host != "local":
            line = f"{site_label(host)}: {line}"
        self.log(line, progress=("build", host) if compiling else None)

    def start(self, title, function, *, cancellable=False, action=None):
        with self.lock:
            if self.stopping:
                raise ValueError("MTS is shutting down.")
            if self.task["state"] == "running" or self.monitor.local_inventory_lock.locked():
                raise ValueError("An operation is already running. Wait for it to finish.")
            self.task = {"id": secrets.token_hex(8), "state": "running", "title": title,
                         "created_at": time.time() * 1000, "logs": [], "result": None,
                         "cancellable": cancellable, "action": action}
            self.progress_lines.clear()
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
                outcome = ("submitted" if isinstance(result, dict) and result.get("config_hash") else
                           "completed with errors" if partial else "completed")
                self.log(f"{title}: {outcome} in {time.monotonic() - started:.2f} s")
                with self.lock:
                    self.task.update(state="partial" if partial else "complete", result=result)
            except CancelledError as error:
                self.log(str(error))
                with self.lock:
                    self.task.update(state="aborted")
            except Exception as error:
                # The manager may be absent during initial GUI setup.
                errors = sys.modules.get("MTS_Management.errors")
                if errors and isinstance(error, errors.ExpectedError):
                    self.log(str(error))
                    with self.lock:
                        self.task.update(state="failed", error=str(error), warning=True)
                    return
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

    def abort(self, task_id=None):
        with self.lock:
            if task_id is not None and (self.task.get("id") != task_id or self.task["state"] != "running"):
                raise ValueError("The selected operation has already ended.")
            if self.task["state"] != "running" or not self.task.get("cancellable"):
                raise ValueError("This operation cannot be safely interrupted.")
            self.task["cancel_requested"] = True
            self.cancel.set()

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

    def clear_terminal(self, entry_id=None):
        with self.lock:
            self.history = [entry for entry in self.history
                            if (entry["id"] != entry_id if entry_id else entry["state"] == "running")]

    def campaign_journal(self):
        from MTS_Management.cluster import load_sites
        from MTS_Management.journal import Journal
        from MTS_Management.submission import submission_state_dir

        path = ROOT / "MTSManager/sites.toml"
        return Journal(submission_state_dir(path, load_sites(path) if path.is_file() else {}))

    def started_campaigns(self):
        journal = self.campaign_journal()
        records = journal.records() if journal.path.is_file() else []
        # Temporary compatibility for receipts written before pre-launch rejection had its own event.
        rejected = {entry.get("submission_id") for entry in records
                    if entry["event"] in {"submission_rejected", "submission_skipped", "submission_conflict"}
                    or (entry["event"] == "submission_error" and entry.get("error") ==
                        "Build MTS2D at the execution site to use the current configuration.")}
        return {entry["campaign"] for entry in records if entry.get("campaign") and (
            entry["event"] in {"submission_accepted", "submitted", "queued", "running"}
            or (entry["event"] == "submission_requested" and entry["event_id"] not in rejected))}

    def campaign_preflight(self, data):
        from .submission import check_campaign

        name = simulation_setup.checked_name(data["name"])
        path = self.monitor.campaign_path(name)
        check_campaign(ROOT / "MTSManager/sites.toml", path, data["site"])
        return {"ready": True}

    def start_options(self):
        from MTS_Management.config import DEFAULTS
        return {"defaults": DEFAULTS,
                "presets": simulation_setup.preset_names(STATE / "simulation-presets"),
                "campaign_sources": sorted(self.monitor.campaign_definitions()),
                "started_campaigns": sorted(self.started_campaigns()),
                "campaigns": sorted(path.parent.name for path in
                                    (ROOT / "MTSManager/campaigns/gui").glob("*/simulations.json"))}

    def mesh_preview(self, data):
        from MTS_Management.cluster import call, load_sites
        from MTS_Management.errors import ExpectedError

        config = simulation_setup.simulation_config(data)
        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        site = profiles.get(data.get("site"))
        if site is None:
            raise ExpectedError("Choose an execution site for the mesh preview.")
        try:
            result = call(site, "mesh_preview", settings=config.as_dict(), timeout=45)
        except RuntimeError as error:
            if "Unknown remote operation: mesh_preview" not in str(error):
                raise
            raise ExpectedError(f"Update the manager on {site_label(site.name)} using Deploy in SSH connections.") from error
        return result["vtu"].encode("ascii")

    def load_campaign(self, name):
        return simulation_setup.campaign_editor(self.monitor.campaign_path(name))

    def campaign_size(self, data):
        source_name = data.get("source_campaign")
        source = self.monitor.campaign_path(source_name) if source_name else None
        return {"jobs": simulation_setup.campaign_size(data, source=source)}

    def save_campaign(self, data):
        from MTS_Management.files import locked, read_json, write_json

        source_name = data.get("source_campaign")
        source = self.monitor.campaign_path(source_name) if source_name else None
        definition, recipe = simulation_setup.campaign_definition(STATE / "simulation-presets", data, source=source)
        name = definition["name"]
        directory = ROOT / "MTSManager/campaigns/gui" / name
        resources = {}
        if source is not None and (path := source.with_name("resources.json")).is_file():
            saved = read_json(path)
            resources = {key: saved[key] for key in ("site", "resources") if key in saved}
        if site := data.get("site"):
            resources["site"] = site
        with locked(STATE / ".simulation-campaigns.lock"):
            if name in self.monitor.campaign_definitions():
                if self.monitor.campaign_path(name) != directory / "simulations.json" or not data.get("update"):
                    raise ValueError("A campaign with this name already exists. Choose a new name.")
                if name in self.started_campaigns():
                    raise ValueError("This campaign name already exists and its jobs have been submitted or may have been accepted. Choose a new name to save an edited copy.")
                previous = directory / "resources.json"
                if previous.is_file():
                    resources = read_json(previous) | resources
            metadata = directory / "metadata.json"
            descriptions = read_json(metadata) if metadata.is_file() else {"missing_settings": {}, "executables": []}
            write_json(directory / "resources.json", resources)
            write_json(metadata, descriptions | {"sweep": recipe})
            write_json(directory / "simulations.json", definition)
        return {"name": name, "runs": len(definition["runs"])}

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
            raise ValueError("Choose an enabled Slurm profile.")
        command = [sys.executable, "-m", "MTS_Management.slurm_control", "--sites", str(ROOT / "MTSManager/sites.toml"),
                   "--site", data["site"], "--action", rule["action"], "--match", rule["match"],
                   "--criteria", json.dumps(rule["criteria"])]
        return {"review": shlex.join(command), "apply": shlex.join(command + ["--apply"])}

    def status(self):
        modules = {"MTSManager": "MTS_Management", "MTSPlot": "MTS_Plotting", "Model math": "MTM_Math"}
        packages = {name: importlib.util.find_spec(module) is not None for name, module in modules.items()}
        site_file = ROOT / "MTSManager/sites.toml"
        sites = []
        servers = []
        build_status = "missing"
        local_current_configuration = False
        if packages["MTSManager"] and site_file.exists():
            from MTS_Management.cluster import load_sites, paths
            profiles = load_sites(site_file)
            seen = set()
            for profile in profiles.values():
                info = {"name": profile.name, "label": site_label(profile.name),
                        "remote": bool(profile.ssh), "mode": profile.mode}
                if not profile.ssh:
                    output = paths(profile)["output_root"]
                    info.update(output_root=output, output_available=not profile.require_output_root or Path(output).is_dir())
                sites.append(info)
                host = profile.ssh or profile.name
                host_key = (bool(profile.ssh), host)
                if host_key not in seen:
                    servers.append({"name": profile.name, "label": site_label(host),
                                    "profiles": [p.name for p in profiles.values() if p.ssh == profile.ssh] if profile.ssh
                                    else [profile.name]})
                    seen.add(host_key)
            if "local" in profiles and not profiles["local"].ssh:
                build_status, local_current_configuration = local_build_status(paths(profiles["local"]))
        with self.lock:
            task = dict(self.task, logs=list(self.task["logs"]))
            history = [{k: list(v) if k == "logs" else v for k, v in entry.items() if k != "result"}
                       for entry in self.history]
        return {"workspace": str(ROOT), "python": sys.version.split()[0],
                "components": {name: (ROOT / name / ("CMakeLists.txt" if name == "MTS2D"
                                                    else "pyproject.toml")).exists() for name in COMPONENTS},
                "packages": packages, "local_build": build_status != "missing",
                "local_build_status": build_status, "local_current_configuration": local_current_configuration,
                "data_folders": [str(p) for p in [*(Path(s["output_root"]) for s in sites if not s["remote"]), *sorted(
                    (ROOT / "MTSPlot/data").glob("*"))] if p.is_dir()],
                "campaigns": sorted(self.monitor.campaign_definitions()) if packages["MTSManager"] else [],
                "sites": sites, "servers": servers, "task": task, "history": history, "interface_version": 4,
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
        if action == "local_output":
            from MTS_Management.cluster import load_sites
            from MTS_Management.local_output import change_local_output
            site_file = ROOT / "MTSManager/sites.toml"
            result = change_local_output(site_file, **data, log=self.log)
            name = result["site"]
            profile = load_sites(site_file)[name]
            try:
                rows = self.monitor.read_inventory(name, profile)
                self.monitor.inventory_cache(name, rows=rows, site=profile)
                with self.lock:
                    self.task["inventory_revision"] = 1
            except Exception as error:
                self.log(f"Refresh Monitor data to update {site_label(name)}. {error}\n{traceback.format_exc()}")
            return {"local_output": result, "errors": result["errors"]}
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
            if action == "ssh_build":
                from MTS_Management.deployments import build_servers
                return build_servers(site_file, host=host, log=self.log, build_log=self.log_build)
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
            return build_simulator(self.log_build)
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
        if action == "build_servers":
            from MTS_Management.deployments import build_servers
            return build_servers(ROOT / "MTSManager/sites.toml", log=self.log, build_log=self.log_build)
        if action in {"inventory", "hard_refresh"}:
            return self.monitor.refresh_inventory(action, data)
        if action in {"logs", "stop"}:
            return self.manager_call(data.get("site", "local"), action, attempt=data["attempt"])
        if action == "continuation_plan":
            row = self.monitor.continuation_source(data)
            keys = ("folder", "config_hash", "manifest_sha256")
            from MTS_Management.errors import ExpectedError
            try:
                result = self.manager_call(row["site"], "continuation_plan", timeout=600,
                                           selection={key: row[key] for key in keys})
            except RuntimeError as error:
                if "Unknown remote operation: continuation_plan" not in str(error):
                    raise
                raise ExpectedError("Update the manager on the execution site to use Load further.") from error
            if "error" in result:
                raise ExpectedError(result["error"])
            return {"continuation_plan": result | {"site": row["site"], "selection": data}}
        if action == "remove_missing":
            return self.monitor.remove_missing_runs(data)
        if action == "resume":
            from MTS_Management.errors import ExpectedError
            row = self.monitor.continuation_source(data)
            if row["status"]["state"] not in {"failed", "stopped"}:
                raise ExpectedError("Select one run whose latest attempt failed or stopped.")
            selection = {key: row[key] for key in ("folder", "config_hash", "manifest_sha256")}
            try:
                result = self.manager_call(row["site"], "resume", timeout=120, selection=selection)
            except RuntimeError as error:
                if "Unknown remote operation: resume" not in str(error):
                    raise
                raise ExpectedError(f"Update the manager on {site_label(row['site'])} using Deploy in SSH connections.") from error
            if result.get("error"):
                raise ExpectedError(result["error"])
            return {"config_hash": row["config_hash"]}
        if action == "run":
            config = simulation_setup.simulation_config(data)
            if continuation := data.get("continuation"):
                if "initial_mesh" in data:
                    raise ValueError("A continuation uses the original initial mesh.")
                row = self.monitor.continuation_source(continuation["selection"])
                if data["site"] != row["site"]:
                    raise ValueError("Continue loading on the original execution site.")
                source = {key: row[key] for key in ("folder", "config_hash", "manifest_sha256")}
                source.update({key: continuation[key] for key in ("checkpoint", "checkpoint_catalog")
                               if key in continuation})
                self.log(f"Preparing a new run on {site_label(row['site'])}.")
                result = self.manager_call(row["site"], "run", timeout=3600,
                                           settings=config.as_dict(), label=data.get("label") or None,
                                           campaign="GUI", continuation=source)
                self.log(f"Continuation submitted in {result['output_folder']}.")
                return {"config_hash": config.config_hash}
            from .submission import start_run
            if campaign := data.get("campaign"):
                self.monitor.campaign_path(campaign)
            return start_run(ROOT / "MTSManager/sites.toml", config, data, self.log)
        if action == "run_campaign":
            from MTS_Management.files import locked
            from .submission import start_campaign
            site_file = ROOT / "MTSManager/sites.toml"
            with locked(STATE / ".simulation-campaigns.lock"):
                self.campaign_preflight(data)
                path = self.monitor.campaign_path(data["name"])
                return start_campaign(site_file, path, data["site"], self.log,
                                      report_progress=lambda line: self.log(line, progress="campaign"))
        if action == "plot_monitor":
            return self.plots.open_run_plot(data)
        if action == "plot_csv_refresh":
            return self.plots.refresh_campaign_csvs(data)
        if action == "animate":
            return self.plots.render_animation(data)
        if action == "animate_preview":
            return self.plots.render_animation(data, preview=True)
        if action == "archive_monitor":
            return self.monitor.archive_monitor(data)
        if action == "restore_archive":
            return self.monitor.restore_archive(data["id"])
        if action == "archive_campaign":
            from MTS_Management.errors import ExpectedError
            name = data["campaign"]
            if not self.monitor.archive_campaign(name, self.monitor.campaign_definitions().get(name, {})):
                raise ExpectedError("Campaign settings can be archived after every planned run is archived and no active copies remain.")
            return {"campaign_archived": name}
        if action == "plot":
            return self.plots.export_plot(data)
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
        if unquote(path.path).startswith(("/api/", "/figures/", "/previews/", "/videos/")):
            token = self.headers.get("X-MTS-Token") or parse_qs(path.query).get("token", [""])[0]
            if not secrets.compare_digest(token, self.server.app.token):
                self.reply(403, {"error": "Open MTS using the desktop launcher to start a valid session."})
                return False
        return True

    def serve_video(self, path):
        base = (STATE / "videos").resolve()
        file = (base / path.removeprefix("/videos/")).resolve()
        if not file.is_relative_to(base) or file.suffix != ".mp4":
            raise ValueError("Invalid animation path.")
        with file.open("rb") as stream:
            size = file.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if match and (match[1] or match[2]):
                    if match[1]:
                        start = int(match[1])
                        end = min(end, int(match[2])) if match[2] else end
                    else:
                        start = max(0, size - int(match[2]))
                if not match or not (match[1] or match[2]) or not 0 <= start <= end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            remaining = end - start + 1
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(remaining))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            stream.seek(start)
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(block)

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
                self.reply(200, self.server.app.monitor.inventory_overview(site=site, campaign=campaign))
            elif path == "/api/archive_history":
                self.reply(200, self.server.app.monitor.archive_history())
            elif path == "/api/plot_options":
                query = parse_qs(urlsplit(self.path).query)
                self.reply(200, self.server.app.plots.plot_options(query.get("campaign", [""])[0], query.get("data", [None])[0]))
            elif path == "/api/plot_csv_status":
                query = parse_qs(urlsplit(self.path).query)
                outdated = self.server.app.plots.campaign_csv_outdated(
                    query.get("campaign", [""])[0], query.get("data", [None])[0])
                self.reply(200, {"outdated": outdated})
            elif path == "/api/animation_options":
                query = parse_qs(urlsplit(self.path).query)
                seed = query.get("seed", ["all"])[0]
                self.reply(200, self.server.app.plots.animation_options({
                    "campaign": query.get("campaign", [""])[0], "data": query.get("data", [None])[0],
                    "seed": seed if seed == "all" else int(seed)}, fields=query.get("fields") == ["1"]))
            elif path == "/api/slurm_options":
                self.reply(200, self.server.app.slurm_options())
            elif path == "/api/start_options":
                self.reply(200, self.server.app.start_options())
            elif path == "/api/campaign_settings":
                query = parse_qs(urlsplit(self.path).query)
                self.reply(200, self.server.app.load_campaign(query.get("name", [""])[0]))
            elif path == "/api/plot_check":
                from MTS_Plotting.campaign_plot import prepared_plot, validate_axes
                data = json.loads(parse_qs(urlsplit(self.path).query)["selection"][0])
                _, result = prepared_plot(*self.server.app.plots.plot_inputs(data))
                result = result | {"warnings": result["warnings"] + validate_axes(result, data)}
                self.reply(200, {k: result[k] for k in ("warnings", "runs", "expected", "mode")})
            elif path == "/api/plot_presets":
                self.reply(200, self.server.app.plots.plot_presets())
            elif path == "/api/animation_presets":
                self.reply(200, self.server.app.plots.presets(kind="animation"))
            else:
                if path.startswith("/videos/"):
                    self.serve_video(path)
                    return
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
            action = urlsplit(self.path).path.removeprefix("/api/")
            length = int(self.headers.get("Content-Length", "0"))
            limit = 20 * 1024 * 1024 if action == "run" else 65536
            if not 0 <= length <= limit or self.headers.get("Content-Type") != "application/json":
                raise ValueError(f"Expected a JSON request of at most {limit // 1024:,} KiB.")
            data = json.loads(self.rfile.read(length))
            if action == "slurm_program":
                self.reply(200, self.server.app.slurm_program(data))
            elif action == "inventory_local":
                self.reply(200, self.server.app.monitor.refresh_local_inventory(**data))
            elif action == "mesh_preview":
                self.reply(200, self.server.app.mesh_preview(data), "application/vnd.vtk.vtu")
            elif action == "run_template":
                self.reply(200, self.server.app.monitor.run_template(data))
            elif action == "run_conflict":
                self.reply(200, self.server.app.monitor.run_conflict(data))
            elif action == "plot_preview":
                self.reply(200, self.server.app.plots.plot_preview(data))
            elif action == "plot_presets":
                self.reply(200, self.server.app.plots.plot_presets(data))
            elif action == "animation_presets":
                self.reply(200, self.server.app.plots.presets(data, kind="animation"))
            elif action == "simulation_preset":
                self.reply(200, simulation_setup.preset(STATE / "simulation-presets", data))
            elif action == "open_simulation_folder":
                self.reply(200, self.server.app.plots.open_simulation_folder(data))
            elif action == "pick_simulation_folder":
                self.reply(200, self.server.app.plots.pick_simulation_folder())
            elif action == "plan_local_output":
                from MTS_Management.local_output import plan_local_output
                self.reply(200, plan_local_output(ROOT / "MTSManager/sites.toml", **data))
            elif action == "save_campaign":
                self.reply(200, self.server.app.save_campaign(data))
            elif action == "campaign_preflight":
                self.reply(200, self.server.app.campaign_preflight(data))
            elif action == "campaign_size":
                self.reply(200, self.server.app.campaign_size(data))
            elif action == "open_run_folder":
                self.reply(200, self.server.app.monitor.open_run_folder(data))
            elif action == "terminal":
                self.server.app.clear_terminal(data.get("id"))
                self.reply(200, {"cleared": True})
            elif action == "abort":
                self.server.app.abort(data.get("task_id"))
                self.reply(200, {"requested": True})
            elif action in {"quit", "restart"}:
                with self.server.app.lock:
                    if self.server.app.task["state"] == "running" or self.server.app.monitor.local_inventory_lock.locked():
                        raise ValueError("Wait for the current operation to finish before quitting or restarting MTS.")
                    if self.server.app.stopping:
                        raise ValueError("MTS is already shutting down.")
                    self.server.app.stopping = True
                    self.server.restart_requested = action == "restart"
                self.reply(200, {"restarting": self.server.restart_requested})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif action in {"install", "build", "inventory", "hard_refresh", "update_managers", "logs", "stop", "run", "resume", "run_campaign", "continuation_plan", "plot", "plot_monitor", "plot_csv_refresh", "animate", "animate_preview", "archive_monitor", "archive_campaign", "restore_archive", "remove_missing", "slurm_plan", "slurm_execute", "local_output",
                            "ssh_check", "ssh_add", "ssh_toggle", "ssh_accept", "ssh_install", "ssh_scan", "ssh_configure", "ssh_profile", "ssh_inspect", "ssh_deploy",
                            "ssh_build", "build_servers"}:
                title = {"plot_monitor": "Prepare plot data", "plot_csv_refresh": "Refresh campaign CSVs",
                         "archive_monitor": "Archive", "archive_campaign": "Archive campaign settings",
                         "restore_archive": "Restore", "continuation_plan": "Load further",
                         "run": "Start simulation", "remove_missing": "Remove all missing",
                         "animate": "Generate animation", "animate_preview": "Preview animation",
                         "local_output": "Change local output", "ssh_build": "Build MTS2D",
                         "build_servers": "Build on servers"}.get(action, action.capitalize())
                task_id = self.server.app.start(title, lambda: self.server.app.operation(action, data),
                                                cancellable=action in {"inventory", "hard_refresh", "logs", "plot_csv_refresh", "animate", "animate_preview"},
                                                action=action)
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
        app.plots.restart_plot_sources(restore=True)
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
        app.plots.close()
        server.server_close()
        if server.restart_requested:
            try:
                app.plots.restart_plot_sources()
            except (OSError, ValueError, TypeError) as error:
                print(f"Monitor selections could not be saved for restart. {error}", file=sys.stderr, flush=True)
        else:
            record.unlink(missing_ok=True)
            write_entry()
    return server.restart_requested
