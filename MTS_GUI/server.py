"""A loopback-only HTTP interface. Simulation lifecycle stays with MTSManager."""

import importlib.util
from contextlib import closing, nullcontext
from concurrent.futures import CancelledError, ProcessPoolExecutor, TimeoutError as FutureTimeout
import multiprocessing
from dataclasses import asdict
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .install import ROOT, STATE, COMPONENTS, build_simulator, install_packages, run, write_entry
from . import simulation_setup


def site_label(name):
    return str(name).upper()


def local_build_status(site_paths):
    from MTS_Management import provenance
    from MTS_Management.files import read_json

    executable = Path(site_paths["executable"])
    manifest = executable.parent / "build-manifest.json"
    if not executable.is_file() or not manifest.is_file():
        return "missing", False, False
    try:
        record = read_json(manifest)
    except (OSError, ValueError):
        return "missing", False, False
    if not isinstance(record, dict) or not record.get("revision"):
        return "missing", False, False

    source = Path(site_paths["source"])
    try:
        if (source / ".git").exists():
            revision = provenance.command(
                ["git", "-C", str(source), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        else:
            revision = read_json(source / "SOURCE.json")["revision"]
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError):
        revision = None
    supports_rectangle = getattr(provenance, "supports_rectangle_boundary", None)
    supports_indentation = getattr(provenance, "supports_indentation", None)
    rectangle = bool(supports_rectangle and supports_rectangle(record))
    indentation = bool(supports_indentation and supports_indentation(record))
    current = supports_indentation or supports_rectangle
    outdated = (revision and record["revision"] != revision) or (current and not current(record))
    return ("outdated" if outdated else "installed"), rectangle, indentation


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
        self.run_plot_sources = {}
        self.animation_memory = None
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
        from MTS_Management.data import seed_group_fields
        definitions = {}
        for path in sorted((ROOT / "MTSManager/campaigns").rglob("simulations.json")):
            stamp = path.stat().st_mtime_ns
            if path not in self.campaign_files or self.campaign_files[path][0] != stamp:
                name, expected = campaign_identity(path)
                runs = json.loads(path.read_text())["runs"]
                groups = {h: seed_group_fields(run["settings"]) for h, run in zip(expected, runs)}
                self.campaign_files[path] = (stamp, name, expected, groups)
            _, name, expected, _ = self.campaign_files[path]
            if name in definitions and definitions[name] != expected:
                raise ValueError(f"Conflicting campaign definitions: {name}")
            definitions[name] = expected
        return definitions

    def local_copies(self):
        """Inspect managed download collections, without traversing simulation output trees."""
        from MTS_Management.archive import ARCHIVE_MARKER
        from MTS_Management.config import Config
        from MTS_Management.data import seed_group_fields
        from MTS_Management.files import file_digest, read_json
        from MTS_Management.inventory import progress, config_metadata_stamp
        rows = []
        receipts = {}
        for manifest in sorted((ROOT / "MTSManager/.state/retrieved").glob("*/*/manifest.json")):
            folder = manifest.parent
            if (folder.parent / ARCHIVE_MARKER).is_file():
                continue
            termination_path = folder / "termination.json"
            receipt_path = folder.parent / "snapshot.json"
            paths = [manifest, folder / "config.conf", folder / "macroData.csv", termination_path, receipt_path]
            stamp = tuple(p.stat().st_mtime_ns if p.exists() else None for p in paths) + (tuple(tuple(v) for v in config_metadata_stamp()),)
            if manifest not in self.local_files or self.local_files[manifest][0] != stamp:
                record = read_json(manifest)
                config = Config.read(folder / "config.conf", expected_name=folder.name)
                if config.config_hash != record["config_hash"]:
                    raise ValueError(f"Local manifest/config mismatch: {folder}")
                termination = read_json(termination_path)["termination"] if termination_path.exists() else None
                request = next(folder.glob("attempts/*/request.json"), None)
                request_data = read_json(request) if request else {}
                origin = request_data.get("site")
                origin_folder = request_data.get("output_folder")
                manifest_sha = file_digest(manifest)
                if receipt_path not in receipts:
                    receipts[receipt_path] = read_json(receipt_path) if receipt_path.exists() else {}
                receipt = receipts[receipt_path]
                if any(run["config_hash"] == config.config_hash and run["manifest_sha256"] == manifest_sha
                       for run in receipt.get("runs", [])):
                    origin = receipt["site"]
                    origin_folder = str(Path(receipt["source_root"]) / folder.name)
                row = {"config_hash": config.config_hash, "folder": str(folder), "attempt": None,
                       **seed_group_fields(config.as_dict()),
                       "campaign": record.get("campaign"), "manifest_sha256": manifest_sha,
                       "executable_sha256": record.get("executable_sha256"),
                       "run_date": record.get("created_at"),
                       "status": {"state": "finished" if termination else "local copy"},
                       "termination": termination, "progress": progress(folder, config),
                       "site": None, "origin_site": origin, "local_copy": True,
                       "origin_folder": origin_folder,
                       "origin_state_dir": request_data.get("state_dir", ""),
                       "local_updated_at": max(v for v in stamp[:-1] if v is not None) / 1e9}
                self.local_files[manifest] = (stamp, row)
            rows.append(dict(self.local_files[manifest][1]))
        return rows

    def inventory_overview(self, *, site=None, campaign=None):
        from MTS_Management.cluster import load_sites, shell_path
        from MTS_Management.data import seed_group_fields
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
            row for snapshot in snapshots.values() for row in snapshot["rows"]]
            if row.get("campaign") and not row.get("archived")})
        config_hashes = sorted(set(expected) | {row["config_hash"] for row in copies + [
            row for snapshot in snapshots.values() for row in snapshot["rows"]] if row.get("campaign") == campaign}) if campaign is not None else None
        if campaign is not None:
            snapshots = {name: self.inventory_cache(name, site=profile, config_hashes=config_hashes)
                         for name, profile in profiles.items()}
        selected = ({name: snapshot for name, snapshot in snapshots.items()
                     if profiles[name].ssh == profiles[site].ssh} if site is not None else
                    snapshots)
        rows = [row | {"site": name} for name, snapshot in selected.items() for row in snapshot["rows"]
                if not row.get("archived") and (campaign is None or matches(row))]
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
        found = {row["config_hash"] for row in rows if not row.get("missing")}
        archived = {row["config_hash"] for snapshot in snapshots.values() for row in snapshot["rows"]
                    if row.get("archived")} if expected else set()
        if expected:
            archived.update(self.archived_downloads())
        archived &= expected.keys()
        missing = set(expected) - found - archived
        registered_missing = {row["config_hash"] for row in rows if row.get("missing")}
        rows.extend({"config_hash": h, "folder": expected[h], "site": None, "attempt": None,
                     "campaign": campaign, "status": {"state": "missing"}, "termination": None,
                     "progress": {"fraction": None}, "missing": True} for h in sorted(missing - registered_missing))
        groups = {h: group for _, _, _, saved in self.campaign_files.values() for h, group in saved.items()}
        for row in rows:
            if row.get("local_copy"):
                profile = profiles.get(row.get("origin_profile") or row["origin_site"])
                remote_folder = row.get("origin_folder")
            else:
                profile = profiles.get(row["site"])
                remote_folder = row["folder"]
            if profile and profile.ssh and remote_folder:
                command = f'cd -- {shell_path(remote_folder)} && exec "$SHELL" -i'
                row["ssh_command"] = shlex.join(["ssh", "-t", "--", profile.ssh, command])
            if row["config_hash"] not in groups and not row.get("settings_group"):
                path = STATE / "configurations" / (row["config_hash"] + ".conf")
                if not path.exists() and row.get("site") and not profiles[row["site"]].ssh:
                    path = Path(row["folder"]) / "config.conf"
                if path.exists():
                    from MTS_Management.config import Config
                    config = Config.read(path)
                    if config.config_hash != row["config_hash"]:
                        raise ValueError(f"Saved configuration hash mismatch: {path}")
                    groups[row["config_hash"]] = seed_group_fields(config.as_dict())
            row.update(groups.get(row["config_hash"], {}))
        sources = [{"site": name, "label": site_label(name), "updated_at": snapshot["updated_at"]}
                   for name, snapshot in selected.items()]
        updated = [source["updated_at"] for source in sources if source["updated_at"] is not None]
        return {"site": site, "campaign": campaign, "rows": rows, "sources": sources,
                "updated_at": min(updated) if updated else None, "campaigns": campaigns,
                "config_hashes": config_hashes,
                "expected": len(expected) if campaign in definitions else None,
                "found": len(set(expected) & found), "missing": len(missing),
                "archived": len(archived - found), "archived_hashes": sorted(archived),
                "uncached_sites": [site_label(name) for name, snapshot in snapshots.items()
                                   if snapshot["updated_at"] is None]}

    def open_run_folder(self, data):
        from MTS_Management.cluster import load_sites, paths
        from MTS_Management.files import read_json

        folder = Path(data["folder"]).resolve()
        site = data.get("site")
        if site is None:
            root = (ROOT / "MTSManager/.state/retrieved").resolve()
            valid = folder.parent.parent == root
        else:
            profile = load_sites(ROOT / "MTSManager/sites.toml").get(site)
            valid = bool(profile and not profile.ssh and folder.parent == Path(paths(profile)["output_root"]))
        if not valid or not folder.is_dir():
            raise ValueError("Local simulation folder is unavailable. Refresh Monitor data.")
        if read_json(folder / "manifest.json")["config_hash"] != data["config_hash"]:
            raise ValueError("Local simulation identity changed. Refresh Monitor data.")
        command = ["/usr/bin/open", "-a", "Finder"] if sys.platform == "darwin" else ["xdg-open"]
        subprocess.run([*command, str(folder)], check=True, capture_output=True, text=True, timeout=10)
        return {"opened": True}

    def clear_terminal(self, entry_id=None):
        with self.lock:
            self.history = [entry for entry in self.history
                            if (entry["id"] != entry_id if entry_id else entry["state"] == "running")]

    def campaign_path(self, name):
        if name not in self.campaign_definitions():
            raise ValueError("Select a registered campaign.")
        return next(path for path, (_, campaign, _, _) in self.campaign_files.items()
                    if campaign == name and path.is_file())

    def selected_inventory_runs(self, data):
        from MTS_Management.errors import ExpectedError
        rows = self.inventory_overview(**data["scope"])["rows"]
        key = lambda row: (row["site"], row["folder"], row["config_hash"], row.get("manifest_sha256"))
        available = {key(row): row for row in rows if not row.get("missing")}
        selected = [key(row) for row in data["runs"]]
        if not selected or len(set(selected)) != len(selected) or not set(selected) <= available.keys():
            raise ExpectedError("The selection changed. Refresh Monitor data and select the runs again.")
        return [available[k] for k in selected]

    def continuation_source(self, selection):
        from MTS_Management.errors import ExpectedError

        rows = self.selected_inventory_runs(selection)
        if (len(rows) != 1 or not rows[0]["site"] or
                rows[0]["status"]["state"] not in {"finished", "failed", "stopped"}):
            raise ExpectedError("Select one stopped run on its execution site.")
        return rows[0]

    def remove_missing_runs(self, scope):
        from MTS_Management.cluster import load_sites
        from MTS_Management.errors import ExpectedError

        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        groups = {}
        for row in self.inventory_overview(**scope)["rows"]:
            if row.get("missing") and row["site"]:
                groups.setdefault(row["site"], []).append(
                    {key: row[key] for key in ("folder", "config_hash")})
        removed, errors = 0, {}
        for name, runs in groups.items():
            try:
                result = self.manager_call(name, "remove_missing", runs=runs)
                hashes = set(result["removed"])
                removed += len(hashes)
                keys = {(row["folder"], row["config_hash"]) for row in runs if row["config_hash"] in hashes}
                for alias, profile in profiles.items():
                    if profile.ssh != profiles[name].ssh:
                        continue
                    overlap = {row["config_hash"] for row in self.inventory_cache(alias, site=profile)["rows"]
                               if (row["folder"], row["config_hash"]) in keys}
                    if overlap:
                        self.inventory_cache(alias, rows=[], site=profile, config_hashes=overlap)
            except (ConnectionError, subprocess.SubprocessError, RuntimeError) as error:
                detail = (f"Update the manager on {site_label(name)} in Setup and tools."
                          if "Unknown remote operation: remove_missing" in str(error) else str(error))
                errors[name] = detail
                self.log(detail)
        if errors and not removed:
            raise ExpectedError(next(iter(errors.values())))
        self.log(f"Removed {removed} missing run records from the inventory.")
        return {"missing_removed": removed, "errors": errors}

    def run_plot_source(self, selection):
        from MTS_Management.errors import ExpectedError

        if selection not in self.run_plot_sources:
            raise ExpectedError("Select the simulation again in Monitor data.")
        return self.run_plot_sources[selection]

    def restart_plot_sources(self, *, restore=False):
        path = STATE / "restart-plot-sources.json"
        if not restore:
            sources = {key: source | {"path": str(source["path"])} for key, source in self.run_plot_sources.items()}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "sources": sources}))
            temporary.replace(path)
            return
        try:
            saved = json.loads(path.read_text())
            if saved["version"] != 1:
                raise ValueError("Unsupported saved Monitor selection format.")
            sources = {}
            for key, source in saved["sources"].items():
                if (not key.startswith("@") or set(source) != {"path", "manifests", "group", "site", "anchor"}
                        or not isinstance(source["manifests"], dict) or not isinstance(source["group"], list)
                        or not isinstance(source["anchor"], dict)):
                    raise ValueError("Invalid saved Monitor selection.")
                sources[key] = source | {"path": Path(source["path"])}
            self.run_plot_sources = sources
            path.unlink()
        except FileNotFoundError:
            return
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            self.history.append({"id": secrets.token_hex(8), "title": "Recover workspace", "state": "failed",
                                 "created_at": time.time() * 1000,
                                 "logs": [f"Monitor selections could not be restored. {error}"]})

    def open_run_plot(self, data):
        from MTS_Management.cluster import fetch_many, load_sites, paths
        from MTS_Management.data import plot_seed_group
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import digest, file_digest

        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        previous = data.get("selection")
        if previous:
            source = self.run_plot_source(previous)
            profile = profiles.get(source["site"])
            if source["site"] is not None and profile is None:
                raise ExpectedError("The server profile is unavailable. Select the simulation again in Monitor data.")
            anchor, group, local_run = (source[k] for k in ("anchor", "group", "path"))
            include_seeds = data["include_seeds"]
            if type(include_seeds) is not bool:
                raise ValueError("Include other seeds must be true or false.")
        else:
            rows = [{key: row[key] for key in ("site", "folder", "config_hash", "manifest_sha256")}
                    for row in self.selected_inventory_runs(data)]
            anchor = rows[0]
            profile = profiles.get(anchor["site"])
            origin = Path(anchor["folder"])
            identity = {"folder": str(origin), "manifest_sha256": anchor["manifest_sha256"]}
            if profile and profile.ssh:
                try:
                    result = self.manager_call(profile.name, "plot_seed_group", site=profile, **identity)
                except RuntimeError as error:
                    if "Unknown remote operation: plot_seed_group" not in str(error):
                        raise
                    self.log(f"Update the manager on {site_label(profile.name)} to detect other seeds on the server.")
                    result = {"runs": rows}
                if result.get("error"):
                    raise ExpectedError(result["error"])
                group = result["runs"]
                collection = digest([asdict(profile), sorted(row["manifest_sha256"] for row in group)])
                local_run = STATE / "plot-downloads" / collection / origin.name
            else:
                root = Path(paths(profile)["output_root"]) if profile else origin.parent
                group = plot_seed_group(root, **identity)
                local_run = origin
            members = {(row["folder"], row["manifest_sha256"]) for row in group}
            if any(row["site"] != anchor["site"] or Path(row["folder"]).parent != origin.parent or
                   (row["folder"], row["manifest_sha256"]) not in members for row in rows):
                raise ExpectedError("These runs do not share an output folder and executable. Select an individual run.")
            include_seeds = len(rows) > 1
        chosen = group if include_seeds else [anchor]
        if profile and profile.ssh and (not previous or include_seeds):
            self.log(f"Downloading macro data from {site_label(profile.name)} ({len(chosen)} runs).")
            manifests = {row["config_hash"]: row["manifest_sha256"] for row in chosen}
            fetch_many(profile, manifests, local_run.parent, expected_manifests=manifests, retain_binaries=False)
        run_manifests = {}
        for row in chosen:
            folder = local_run.parent / Path(row["folder"]).name
            manifest = folder / "manifest.json"
            if not manifest.is_file() or file_digest(manifest) != row["manifest_sha256"]:
                raise ExpectedError(f"Run identity changed: {folder}. Select the simulation again in Monitor data.")
            run_manifests[folder.name] = row["manifest_sha256"]
        selection = "@" + secrets.token_hex(16)
        self.run_plot_sources[selection] = {"path": local_run, "manifests": run_manifests, "group": group,
                                            "site": profile.name if profile and profile.ssh else None,
                                            "anchor": anchor}
        return {"plot_selection": selection, "plot_name": local_run.name.rsplit("[", 1)[0],
                "has_other_seeds": len(group) > 1, "include_seeds": include_seeds,
                "previous_selection": previous}

    def plot_inputs(self, data):
        """Resolve a campaign, local file or verified Monitor selection."""
        name, root = data["campaign"], data.get("data")
        spec = {k: v for k, v in data.items() if k not in {"campaign", "data", "automatic", "run_manifests"}}
        if name == "@csv":
            path = self.local_data_file(root, {".csv"})
            return path, path, spec
        if name == "@mesh":
            from MTS_Management.files import file_digest

            folder = self.mesh_file_run(root)
            return folder, folder, spec | {"run_manifests": {folder.name: file_digest(folder / "manifest.json")}}
        if not name.startswith("@"):
            return self.campaign_path(name), root, spec
        source = self.run_plot_source(name)
        if root and Path(root).resolve() != source["path"]:
            raise ValueError("Select the simulation again in Monitor data.")
        return source["path"], source["path"], spec | {"run_manifests": source["manifests"]}

    def animation_source(self, data):
        from MTS_Management.campaign import campaign_entries, campaign_identity
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import file_digest
        from MTS_Plotting.campaign_plot import matching_folders

        name = data["campaign"]
        if not name or name == "@csv":
            return None
        if name == "@mesh":
            return self.mesh_file_run(data["data"])
        source, root, spec = self.plot_inputs(data)
        if name.startswith("@"):
            if len(spec["run_manifests"]) != 1:
                return None
            manifest = source / "manifest.json"
            if not manifest.is_file() or file_digest(manifest) != spec["run_manifests"][source.name]:
                raise ExpectedError("The simulation changed. Select it again in Monitor data.")
            return source
        if root:
            seed = data.get("seed", "all")
            if seed == "all":
                _, expected = campaign_identity(source)
            elif type(seed) is int:
                _, entries = campaign_entries(source)
                expected = {config.config_hash for config, _ in entries if config["seed"] == seed}
            else:
                raise ValueError("Select a saved seed or all seeds.")
            folders = matching_folders(Path(root).expanduser().resolve(), expected)
            if len(folders) == 1:
                return folders[0]
        return None

    def animation_options(self, data):
        from MTS_Plotting.runs import Run

        folder = self.animation_source(data)
        if folder is None:
            return {}
        config = Run.read(folder).config
        return {"boundary": config["boundary"], "square_model": config["energyFunction"] == "contiSquare",
                "delaunay_dislocations": config["reconnectionMethod"] == "none"}

    def render_animation(self, data, *, preview=False):
        from MTS_Management.cluster import fetch_many, load_sites
        from MTS_Management.data import mesh_snapshots
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import file_digest
        from MTS_Plotting.mesh_movie import (
            ENERGY_RANGE_PROGRESS, display_limit_sources, encoder_path, frame_schedule, tiled_display,
        )
        from MTS_Plotting.runs import Run

        folder = self.animation_source(data)
        if folder is None:
            raise ExpectedError("Select one simulation to generate an animation.")
        selection = {"campaign": data["campaign"], "data": data["data"], "seed": data.get("seed", "all")}

        def log_animation(line):
            if line.startswith(ENERGY_RANGE_PROGRESS):
                energy_range = json.loads(line.removeprefix(ENERGY_RANGE_PROGRESS))
                with self.lock:
                    self.task["animation_scan"] = {"animation_selection": selection, "energy_range": energy_range}
            self.log(line)

        override = STATE / "ffmpeg-path"
        encoder = None if preview else encoder_path(override.read_text().strip() if override.is_file() else None)
        spec = {k: data[k] for k in ("kind", "matrix", "tiling", "playback", "fps", "seconds_per_load", "minimum_seconds", "limit")}
        spec.update({key: data[key] for key in ("dpi", "heatmap", "node_colours", "node_size", "show_nodes",
                                                "show_mesh_lines", "workers", "legend_size", "show_title",
                                                "title_size", "show_border", "show_legend", "dislocations",
                                                "dislocation_lattice", "dislocation_method",
                                                "dislocation_bond_tolerance", "dislocation_scale",
                                                "dislocation_thickness", "energy_min", "stress_component", "opacity",
                                                "opacity_curve") if key in data})
        rendering = {k: v for k, v in spec.items() if k != "workers"}
        source = self.run_plot_sources.get(data["campaign"])
        output = (STATE / "previews" / f"animation-{secrets.token_hex(8)}.png" if preview else
                  STATE / "videos" / secrets.token_hex(8) / "mesh.mp4")
        identity = file_digest(folder / "manifest.json")
        download = tempfile.TemporaryDirectory(dir=STATE, prefix="animation-") if source and source["site"] else nullcontext()
        with download as temporary:
            if temporary:
                site = load_sites(ROOT / "MTSManager/sites.toml").get(source["site"])
                if site is None:
                    raise ExpectedError("The server profile is unavailable. Select the simulation again.")
                anchor = source["anchor"]
                try:
                    result = self.manager_call(site.name, "mesh_snapshots", site=site,
                                               folder=anchor["folder"], manifest_sha256=anchor["manifest_sha256"])
                except RuntimeError as error:
                    if "Unknown remote operation: mesh_snapshots" in str(error):
                        raise ExpectedError("Update the remote manager in Setup and tools to animate its mesh snapshots.") from error
                    raise
                if result.get("error"):
                    raise ExpectedError(result["error"])
                catalog = result["frames"]
            else:
                catalog = mesh_snapshots(folder)
            if not catalog:
                raise ExpectedError("No mesh snapshots are available. Choose a run with saved mesh output.")
            first = catalog[0]["name"]
            if temporary:
                first_revision = None
            else:
                info = (folder / "data" / first).stat()
                first_revision = (info.st_mtime_ns, info.st_size)
            memory_key = (str(folder), identity, first, first_revision, rendering)
            if not preview and self.animation_memory and self.animation_memory[0] == memory_key:
                spec["frame_memory"] = self.animation_memory[1]
            spec.update(preview=preview, frames=catalog)
            if preview:
                spec["preview_frame"] = data.get("preview_frame", "first")
            if temporary:
                indices = frame_schedule(
                    catalog, playback=spec["playback"], fps=spec["fps"],
                    seconds_per_load=spec["seconds_per_load"], minimum_seconds=spec["minimum_seconds"])
                scheduled = [catalog[index] for index in sorted(set(indices))]
                frames = [frame["name"] for frame in scheduled]
                identities = {anchor["config_hash"]: anchor["manifest_sha256"]}

                def download_frames(names):
                    self.log(f"Downloading {len(names)} mesh states from {site_label(site.name)}.")
                    fetched, = fetch_many(site, identities, temporary, expected_manifests=identities,
                                          retain_binaries=False, mesh_frames=names)
                    return fetched

                if preview:
                    position = spec["preview_frame"]
                    if position not in {"first", "middle"}:
                        raise ValueError("Choose the first or middle animation frame.")
                    kind = spec["kind"]
                    limits = {"kind": kind, "tiled": tiled_display(Run.read(folder).config, kind, spec["tiling"]),
                              "limit": spec["limit"], "lower_limit": spec.get("energy_min", 0.)}
                    _, _, scans = display_limit_sources(folder, scheduled, **limits)
                    if not any(scans):
                        chosen = catalog[indices[len(indices) // 2 if position == "middle" else 0]]["name"]
                        folder = download_frames([chosen])
                        _, _, scans = display_limit_sources(folder, scheduled, **limits)
                        frames = [name for name in frames if name != chosen] if any(scans) else []
                if frames:
                    folder = download_frames(frames)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=STATE) as options:
                json.dump(spec, options)
                options.flush()
                env = os.environ | {"MPLBACKEND": "Agg"}
                if encoder:
                    env["MTS_FFMPEG"] = encoder
                result = run([sys.executable, "-m", "MTS_Plotting.mesh_movie", str(folder), str(output), options.name],
                             log_animation, json_result=True, env=env)
        if preview:
            if spec["preview_frame"] == "first":
                self.animation_memory = (memory_key, result["frame_memory"])
            result = {"animation_preview": output.name, "energy_range": result.get("energy_range")}
        else:
            result["video"] = str(output.relative_to(STATE / "videos"))
        return result | {"animation_selection": selection}

    def read_inventory(self, name, profile, **options):
        try:
            return self.manager_call(name, "inventory", site=profile, timeout=60,
                                     include_archived=True, **options)
        except RuntimeError as error:
            if "unexpected keyword argument 'include_archived'" not in str(error):
                raise
            return self.manager_call(name, "inventory", site=profile, timeout=60, **options)

    def archived_downloads(self):
        from MTS_Management.archive import ARCHIVE_MARKER
        from MTS_Management.files import read_json
        root = ROOT / "MTSManager/.state/retrieved"
        return {read_json(p)["config_hash"] for marker in root.glob("*/" + ARCHIVE_MARKER)
                for p in marker.parent.glob("*/manifest.json")}

    def archive_history(self):
        from MTS_Management.cluster import load_sites, paths
        from MTS_Management.journal import Journal

        profiles = load_sites(ROOT / "MTSManager/sites.toml", include_disabled=True)
        journal = Journal(STATE / "archive")
        history = journal.records() if journal.path.is_file() else []
        restored = {row["archive_id"] for row in history if row["event"] == "restored"}
        entries = {row["event_id"]: row for row in history if row["event"] == "archived"}
        local_journals = {}
        for name, site in profiles.items():
            if site.ssh:
                continue
            location = paths(site)
            if location["state_dir"] not in local_journals:
                source = Journal(location["state_dir"])
                local_journals[location["state_dir"]] = source.records() if source.path.is_file() else []
            for row in local_journals[location["state_dir"]]:
                if row["event"] == "archived" and Path(row["output_folder"]).parent == Path(location["output_root"]):
                    entries.setdefault(row["event_id"], row | {"site": name, "kind": "run"})
        result = []
        for key, row in entries.items():
            site = profiles.get(row.get("site"))
            if key in restored:
                status = "Restored"
            elif row.get("site") is not None and (site is None or site.disabled):
                status = "Connection unavailable"
            elif not (site and site.ssh) and not Path(row["archive_folder"]).is_dir():
                status = "Unavailable"
            else:
                status = "Archived"
            result.append(row | {"status": status})
        return sorted(result, key=lambda row: row["timestamp"], reverse=True)

    def restore_archive(self, archive_id):
        from MTS_Management.archive import archive_root, rename_folder, restore_simulation
        from MTS_Management.cluster import load_sites
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import file_digest, locked
        from MTS_Management.journal import Journal

        row = next((entry for entry in self.archive_history() if entry["event_id"] == archive_id), None)
        if row is None or row["status"] != "Archived":
            raise ExpectedError("This archive entry is unavailable. Reload the archive log.")
        original, archived = Path(row["output_folder"]), Path(row["archive_folder"])
        name = row.get("site")
        if row["kind"] == "campaign":
            root = ROOT / "MTSManager/campaigns"
            with locked(STATE / ".simulation-campaigns.lock"):
                if (not original.is_relative_to(root) or original == root or
                        archived != archive_root(root) / original.name):
                    raise ValueError("Invalid campaign archive paths")
                if (not (archived / "simulations.json").is_file() or
                        file_digest(archived / "simulations.json") != row["definition_sha256"]):
                    raise ExpectedError("The archived campaign definition is unavailable or has changed.")
                if row["campaign"] in self.campaign_definitions():
                    raise ExpectedError("A campaign with this name is already registered.")
                original.parent.mkdir(parents=True, exist_ok=True)
                rename_folder(archived, original)
        elif name is None:
            if original.parent.parent != (ROOT / "MTSManager/.state/retrieved").resolve():
                raise ValueError("Invalid download archive paths")
            restore_simulation(original.parent, STATE, row, downloaded=True)
        else:
            profiles = load_sites(ROOT / "MTSManager/sites.toml")
            run = {k: row.get(k) for k in ("config_hash", "manifest_sha256", "output_folder", "archive_folder")}
            try:
                result = self.manager_call(name, "restore", site=profiles[name], timeout=120, run=run)
            except RuntimeError as error:
                if "Unknown remote operation: restore" in str(error):
                    raise ExpectedError(f"Update the manager on {site_label(name)} in Setup and tools, then retry Restore.") from error
                raise
            if result.get("error"):
                raise ExpectedError(result["error"])
        Journal(STATE / "archive").append("restored", archive_id=archive_id)
        self.log(f"Restored: {original}")
        if name is not None:
            hashes = [row["config_hash"]]
            try:
                rows = self.read_inventory(name, profiles[name], config_hashes=hashes)
                self.inventory_cache(name, rows=rows, site=profiles[name], config_hashes=hashes)
            except (RuntimeError, ConnectionError, subprocess.SubprocessError) as error:
                self.log(f"Run restored. Refresh Monitor data to update its record. {error}")
        with self.lock:
            self.task["inventory_revision"] = 1
        return {"restored": str(original)}

    def archive_monitor(self, data):
        from MTS_Management.archive import archive_simulations
        from MTS_Management.cluster import load_sites
        from MTS_Management.errors import ExpectedError
        from MTS_Management.journal import Journal

        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        groups = {}
        for row in self.selected_inventory_runs(data):
            if row["status"]["state"] not in {"finished", "failed", "stopped", "local copy"}:
                raise ExpectedError("Select inactive simulations to archive.")
            location = Path(row["folder"]).parent
            groups.setdefault((row["site"], location), []).append(
                {k: row[k] for k in ("folder", "config_hash", "manifest_sha256")})
        moved, errors, hashes = [], {}, set()
        for (name, location), runs in groups.items():
            try:
                if name is None:
                    if location.parent != (ROOT / "MTSManager/.state/retrieved").resolve():
                        raise ValueError(f"Invalid download location: {location}")
                    folders = archive_simulations(location, STATE, runs, downloaded=True)
                else:
                    result = self.manager_call(name, "archive", site=profiles[name], timeout=120, runs=runs)
                    if result.get("error"):
                        raise ExpectedError(result["error"])
                    folders = result["folders"]
                moved.extend(folders)
                self.log("Archived: " + ", ".join(folders))
                selected = [row["config_hash"] for row in runs]
                hashes.update(selected)
                if name is None or profiles[name].ssh:
                    journal = Journal(STATE / "archive")
                    for row, folder in zip(runs, folders, strict=True):
                        journal.append("archived", kind="run", site=name, archive_folder=folder,
                                       output_folder=row["folder"], config_hash=row["config_hash"],
                                       manifest_sha256=row["manifest_sha256"])
                if name is not None:
                    rows = [{"config_hash": row["config_hash"], "folder": folder, "archived": True}
                            for row, folder in zip(runs, folders, strict=True)]
                    self.inventory_cache(name, rows=rows, site=profiles[name], config_hashes=selected)
                    keys = {(row["folder"], row["manifest_sha256"]) for row in runs}
                    for alias, profile in profiles.items():
                        if alias == name or profile.ssh != profiles[name].ssh:
                            continue
                        previous = self.inventory_cache(alias, site=profile)["rows"]
                        overlap = {row["config_hash"] for row in previous
                                   if (row["folder"], row.get("manifest_sha256")) in keys}
                        if overlap:
                            self.inventory_cache(alias, rows=[row for row in rows if row["config_hash"] in overlap],
                                                 site=profile, config_hashes=overlap)
            except (ExpectedError, ConnectionError, subprocess.SubprocessError, RuntimeError) as error:
                if "Unknown remote operation: archive" in str(error):
                    error = ExpectedError(f"Update the manager on {site_label(name)} in Setup and tools, then retry Archive.")
                errors[name or str(location)] = str(error)
                self.log(str(error))
        if not moved:
            raise ExpectedError(next(iter(errors.values())))
        with self.lock:
            self.task["inventory_revision"] = 1
        campaign_notes = []
        for name, expected in self.campaign_definitions().items():
            if hashes & expected.keys():
                try:
                    self.archive_campaign(name, expected, automatic=True)
                except ExpectedError as error:
                    campaign_notes.append(str(error))
                    self.log(str(error))
        return {"archived": moved, "errors": errors, "campaign_notes": campaign_notes}

    def archive_campaign(self, name, expected, *, automatic=False):
        from MTS_Management.archive import archive_root, move_folder
        from MTS_Management.cluster import across, load_sites
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import file_digest, locked
        from MTS_Management.journal import Journal

        overview = self.inventory_overview(campaign=name)
        if not expected or set(overview["archived_hashes"]) != expected.keys():
            return False
        profiles = load_sites(ROOT / "MTSManager/sites.toml", include_disabled=True)
        if automatic:
            unchecked = sorted(site for site, profile in profiles.items()
                               if profile.disabled or site_label(site) in overview["uncached_sites"])
            if unchecked:
                raise ExpectedError(f"Campaign settings retained for {name}. Verification is pending for {', '.join(unchecked)}. "
                                    "Use Archive campaign settings after these sites are available.")
        results = across(list(profiles.values()), "inventory", include_archived=True, config_hashes=list(expected))
        unverified = sorted(site for site, result in results.items() if "error" in result)
        if unverified:
            for site in unverified:
                self.log(f"{site_label(site)} verification: {results[site]['error']}")
            raise ExpectedError(f"Campaign settings retained for {name}. Could not verify: {', '.join(unverified)}.")
        rows = [row for result in results.values() for row in result["data"]]
        for site, result in results.items():
            self.inventory_cache(site, rows=result["data"], site=profiles[site], config_hashes=list(expected))
        if any(not row.get("archived") for row in rows) or any(
                row["config_hash"] in expected for row in self.local_copies()):
            return False
        if not expected.keys() <= ({row["config_hash"] for row in rows} | self.archived_downloads()):
            return False
        root = ROOT / "MTSManager/campaigns"
        with locked(STATE / ".simulation-campaigns.lock"):
            source = self.campaign_path(name).parent
            if source == root or any(p.parent != source for p in source.rglob("simulations.json")):
                raise ExpectedError(f"Campaign settings retained for {name}. The directory contains other campaign definitions.")
            destination = archive_root(root) / source.name
            identity = file_digest(source / "simulations.json")
            move_folder(source, destination)
            Journal(STATE / "archive").append("archived", kind="campaign", site=None, campaign=name,
                                              output_folder=str(source), archive_folder=str(destination),
                                              definition_sha256=identity)
            self.log(f"Archived campaign settings: {source} → {destination}")
        return True

    def start_options(self):
        from MTS_Management.config import DEFAULTS
        return {"defaults": DEFAULTS,
                "presets": simulation_setup.preset_names(STATE / "simulation-presets"),
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
            raise ExpectedError(f"Update the manager on {site_label(site.name)} in Setup and tools.") from error
        return result["vtu"].encode("ascii")

    def save_campaign(self, data):
        from MTS_Management.files import locked, write_json

        definition = simulation_setup.campaign_definition(STATE / "simulation-presets", data)
        name = definition["name"]
        directory = ROOT / "MTSManager/campaigns/gui" / name
        with locked(STATE / ".simulation-campaigns.lock"):
            if name in self.campaign_definitions():
                raise ValueError("A campaign with this name already exists. Choose a new name.")
            write_json(directory / "resources.json", {})
            write_json(directory / "metadata.json", {"missing_settings": {}, "executables": []})
            write_json(directory / "simulations.json", definition)
        return {"name": name, "runs": len(definition["runs"])}

    def plot_data_roots(self):
        from MTS_Management.cluster import load_sites, paths
        profiles = load_sites(ROOT / "MTSManager/sites.toml")
        sources = [paths(profile)["output_root"] for profile in profiles.values() if not profile.ssh]
        sources += list((ROOT / "MTSPlot/data").glob("*"))
        sources += list((ROOT / "MTSManager/.state/retrieved").glob("*"))
        return sources

    @staticmethod
    def local_data_file(value, extensions):
        path = Path(value or "").expanduser()
        if path.is_absolute():
            path = path.resolve()
        if not path.is_absolute() or path.suffix.lower() not in extensions or not path.is_file():
            raise ValueError("Enter the full path of an existing CSV, PVD or VTU file.")
        return path

    def mesh_file_run(self, value):
        path = self.local_data_file(value, {".pvd", ".vtu"})
        for folder in (path.parent, path.parent.parent):
            if (folder / "manifest.json").is_file() and (folder / "config.conf").is_file():
                return folder
        raise ValueError("Open a mesh file from an MTS run folder containing config.conf and manifest.json.")

    def open_data_file(self, data):
        from MTS_Management.data import mesh_snapshots
        from MTS_Management.files import file_digest
        from MTS_Plotting.runs import Run

        extensions = {".csv", ".pvd", ".vtu"}
        if "path" in data:
            path = self.local_data_file(data["path"], extensions)
        else:
            name, size, checksum = data["name"], data["size"], data.get("sha256")
            if Path(name).name != name or Path(name).suffix.lower() not in extensions or type(size) is not int or size < 0:
                raise ValueError("Drop a CSV, PVD or VTU file.")
            roots = {Path(root).resolve() for root in self.plot_data_roots()}
            roots |= {root.with_name(root.name + "-archive") for root in roots if root.name}
            matches = set()
            for root in roots:
                if not root.is_dir():
                    continue
                for folder in (root, *root.iterdir()):
                    if not folder.is_dir():
                        continue
                    for candidate in (folder / name, folder / "data" / name):
                        if candidate.is_file() and candidate.stat().st_size == size:
                            target = candidate.resolve()
                            if target.suffix.lower() in extensions:
                                matches.add(target)
            if checksum:
                matches = {candidate for candidate in matches if file_digest(candidate) == checksum}
            if not checksum or len(matches) != 1:
                return {"paths": sorted(map(str, matches))}
            path, = matches
        selection = "@csv" if path.suffix.lower() == ".csv" else "@mesh"
        if selection == "@mesh":
            folder = self.mesh_file_run(str(path))
            Run.read(folder)
            if not mesh_snapshots(folder):
                raise ValueError("This run has no saved mesh snapshots.")
        return {"plot_selection": selection, "plot_name": path.name, "path": str(path)}

    def plot_options(self, campaign, data=None):
        from MTS_Plotting.campaign_plot import describe

        source, root, spec = self.plot_inputs({"campaign": campaign, "data": data})
        if "run_manifests" in spec or campaign == "@csv":
            schema = describe(source, [], root, run_manifests=spec.get("run_manifests"))
            return schema | {"data": str(Path(data).expanduser().resolve())} if campaign == "@mesh" else schema
        return describe(source, self.plot_data_roots(), root)

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
        return self.presets(data, kind="plot")

    def presets(self, data=None, *, kind):
        from MTS_Management.files import locked, read_json, write_json, now
        path = STATE / f"{kind}-presets.json"
        with locked(STATE / f".{kind}-presets.lock"):
            saved = read_json(path) if path.exists() else {"version": 1, "presets": []}
            if saved.keys() != {"version", "presets"} or saved["version"] != 1 or not isinstance(saved["presets"], list):
                raise ValueError(f"Invalid saved {kind} presets.")
            if data is not None:
                name, selection = data["name"].strip(), data["selection"]
                if not name or len(name) > 120 or not isinstance(selection, dict):
                    raise ValueError("A preset requires a name of 1–120 characters and settings.")
                if kind == "plot":
                    self.campaign_path(selection["campaign"])
                elif not isinstance(selection.get("fields"), dict) or not isinstance(selection.get("curve"), list):
                    raise ValueError("Invalid animation preset settings.")
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
        source, root, spec = self.plot_inputs(data)
        key = json.dumps((str(source), str(root), spec), sort_keys=True)
        with self.preview_lock:
            if self.preview_pool is None:
                self.preview_pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            if self.preview_future is not None and not self.preview_future.done() and key != self.preview_key:
                return {"busy": True}
            if key != self.preview_key or self.preview_future is None or self.preview_future.done():
                directory = STATE / "previews"
                directory.mkdir(exist_ok=True)
                path = directory / (secrets.token_hex(8) + ".png")
                self.preview_future = self.preview_pool.submit(preview, str(source), str(root), str(path), spec)
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
        build_status = "missing"
        local_rectangle_build = False
        local_indentation_build = False
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
                build_status, local_rectangle_build, local_indentation_build = local_build_status(paths(profiles["local"]))
        with self.lock:
            task = dict(self.task, logs=list(self.task["logs"]))
            history = [{k: list(v) if k == "logs" else v for k, v in entry.items() if k != "result"}
                       for entry in self.history]
        return {"workspace": str(ROOT), "python": sys.version.split()[0],
                "components": {name: (ROOT / name / ("CMakeLists.txt" if name == "MTS2D"
                                                    else "pyproject.toml")).exists() for name in COMPONENTS},
                "packages": packages, "local_build": build_status != "missing",
                "local_build_status": build_status, "local_rectangle_build": local_rectangle_build,
                "local_indentation_build": local_indentation_build,
                "data_folders": [str(p) for p in [ROOT / "MTSManager/output", *sorted(
                    (ROOT / "MTSPlot/data").glob("*"))] if p.is_dir()],
                "campaigns": sorted(self.campaign_definitions()) if packages["MTSManager"] else [],
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
                            rows = self.read_inventory(name, profiles[name], **options)
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
        if action == "continuation_plan":
            row = self.continuation_source(data)
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
            return self.remove_missing_runs(data)
        if action == "resume":
            from MTS_Management.errors import ExpectedError
            row = self.continuation_source(data)
            if row["status"]["state"] != "failed":
                raise ExpectedError("Select one run whose latest attempt failed.")
            selection = {key: row[key] for key in ("folder", "config_hash", "manifest_sha256")}
            try:
                result = self.manager_call(row["site"], "resume", timeout=120, selection=selection)
            except RuntimeError as error:
                if "Unknown remote operation: resume" not in str(error):
                    raise
                raise ExpectedError(f"Update the manager on {site_label(row['site'])} in Setup and tools.") from error
            if result.get("error"):
                raise ExpectedError(result["error"])
            return {"config_hash": row["config_hash"]}
        if action == "run":
            config = simulation_setup.simulation_config(data)
            if continuation := data.get("continuation"):
                if "initial_mesh" in data:
                    raise ValueError("A continuation uses the original initial mesh.")
                row = self.continuation_source(continuation["selection"])
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
            temporary = nullcontext(STATE / "configurations")
            mesh = data.get("initial_mesh")
            if "initial_mesh" in data:
                from MTS_Management.config import Config
                from MTS_Management.errors import ExpectedError
                from MTS_Management.initial_mesh import mesh_checksum

                if config["reconnectionMethod"] == "delaunay":
                    raise ExpectedError("Use None or Edge flip reconnection for a custom initial mesh.")
                config = Config(**(config.as_dict() | {"initialMeshSha256": mesh_checksum(mesh)}))
                temporary = tempfile.TemporaryDirectory(prefix="mts-custom-mesh-")
            with temporary as directory:
                folder = Path(directory)
                folder.mkdir(exist_ok=True)
                path = folder / (config.config_hash + ".conf")
                config.write(path, data.get("label") or None)
                command = ["run", path, "--site", data.get("site", "local"), "--campaign", "GUI"]
                if mesh is not None:
                    from MTS_Management.initial_mesh import MESH_FILE, mesh_bytes

                    vtu = path.with_name(MESH_FILE)
                    vtu.write_bytes(mesh_bytes(mesh))
                    command += ["--initial-mesh", vtu]
                if data.get("label"):
                    command += ["--label", data["label"]]
                if data.get("site", "local") == "local":
                    command += ["--mode", "detached"]
                self.manager(*command)
            return {"config_hash": config.config_hash}
        if action == "run_campaign":
            name = simulation_setup.checked_name(data["name"])
            path = self.campaign_path(name)
            site = data["site"]
            command = ["campaign", path, "--site", site]
            if site == "local":
                command += ["--mode", "foreground", "--workers", "1"]
            self.manager(*command)
            return {"campaign": name}
        if action == "plot_monitor":
            return self.open_run_plot(data)
        if action == "animate":
            return self.render_animation(data)
        if action == "animate_preview":
            return self.render_animation(data, preview=True)
        if action == "archive_monitor":
            return self.archive_monitor(data)
        if action == "restore_archive":
            return self.restore_archive(data["id"])
        if action == "archive_campaign":
            from MTS_Management.errors import ExpectedError
            name = data["campaign"]
            if not self.archive_campaign(name, self.campaign_definitions().get(name, {})):
                raise ExpectedError("Campaign settings can be archived after every planned run is archived and no active copies remain.")
            return {"campaign_archived": name}
        if action == "plot":
            # Matplotlib runs in its own process, isolated from HTTP request threads.
            folder = STATE / "figures" / secrets.token_hex(6)
            path = folder / "campaign-plot.pdf"
            self.log("Reading simulation tables and creating the figure…")
            source, root, spec = self.plot_inputs(data)
            result = run([sys.executable, "-m", "MTS_Plotting.campaign_plot", str(source),
                          str(Path(root).expanduser().resolve()), str(path), json.dumps(spec)],
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
                self.reply(200, self.server.app.inventory_overview(site=site, campaign=campaign))
            elif path == "/api/archive_history":
                self.reply(200, self.server.app.archive_history())
            elif path == "/api/plot_options":
                query = parse_qs(urlsplit(self.path).query)
                self.reply(200, self.server.app.plot_options(query.get("campaign", [""])[0], query.get("data", [None])[0]))
            elif path == "/api/animation_options":
                query = parse_qs(urlsplit(self.path).query)
                seed = query.get("seed", ["all"])[0]
                self.reply(200, self.server.app.animation_options({
                    "campaign": query.get("campaign", [""])[0], "data": query.get("data", [None])[0],
                    "seed": seed if seed == "all" else int(seed)}))
            elif path == "/api/slurm_options":
                self.reply(200, self.server.app.slurm_options())
            elif path == "/api/start_options":
                self.reply(200, self.server.app.start_options())
            elif path == "/api/plot_check":
                from MTS_Plotting.campaign_plot import prepared_plot, validate_axes
                data = json.loads(parse_qs(urlsplit(self.path).query)["selection"][0])
                _, result = prepared_plot(*self.server.app.plot_inputs(data))
                result = result | {"warnings": result["warnings"] + validate_axes(result, data)}
                self.reply(200, {k: result[k] for k in ("warnings", "runs", "expected", "mode")})
            elif path == "/api/plot_presets":
                self.reply(200, self.server.app.plot_presets())
            elif path == "/api/animation_presets":
                self.reply(200, self.server.app.presets(kind="animation"))
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
            elif action == "mesh_preview":
                self.reply(200, self.server.app.mesh_preview(data), "application/vnd.vtk.vtu")
            elif action == "plot_preview":
                self.reply(200, self.server.app.plot_preview(data))
            elif action == "plot_presets":
                self.reply(200, self.server.app.plot_presets(data))
            elif action == "animation_presets":
                self.reply(200, self.server.app.presets(data, kind="animation"))
            elif action == "simulation_preset":
                self.reply(200, simulation_setup.preset(STATE / "simulation-presets", data))
            elif action == "open_data_file":
                self.reply(200, self.server.app.open_data_file(data))
            elif action == "save_campaign":
                self.reply(200, self.server.app.save_campaign(data))
            elif action == "open_run_folder":
                self.reply(200, self.server.app.open_run_folder(data))
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
            elif action in {"install", "build", "inventory", "hard_refresh", "update_managers", "logs", "stop", "run", "resume", "run_campaign", "continuation_plan", "plot", "plot_monitor", "animate", "animate_preview", "archive_monitor", "archive_campaign", "restore_archive", "remove_missing", "slurm_plan", "slurm_execute",
                            "ssh_check", "ssh_add", "ssh_toggle", "ssh_accept", "ssh_install", "ssh_scan", "ssh_configure", "ssh_profile", "ssh_inspect", "ssh_deploy"}:
                title = {"plot_monitor": "Prepare plot data", "archive_monitor": "Archive", "archive_campaign": "Archive campaign settings",
                         "restore_archive": "Restore", "continuation_plan": "Load further",
                         "run": "Start simulation", "remove_missing": "Remove all missing"}.get(action, action.capitalize())
                task_id = self.server.app.start(title, lambda: self.server.app.operation(action, data),
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
        app.restart_plot_sources(restore=True)
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
        if server.restart_requested:
            try:
                app.restart_plot_sources()
            except (OSError, ValueError, TypeError) as error:
                print(f"Monitor selections could not be saved for restart. {error}", file=sys.stderr, flush=True)
        else:
            record.unlink(missing_ok=True)
            write_entry()
    return server.restart_requested
