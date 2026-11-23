"""Monitor selections, desktop inventory snapshots and archive operations."""

from concurrent.futures import CancelledError
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

from . import simulation_setup


def site_label(name):
    return str(name).upper()


class Monitor:
    def __init__(self, workspace, root, state):
        self.workspace = workspace
        self.root = root
        self.state = state
        self.local_inventory_lock = threading.Lock()
        self.campaign_files = {}
        self.local_files = {}

    def inventory_cache(self, site_name, *, rows=None, site=None, config_hashes=None):
        """Merge scoped results atomically, retaining unrelated rows and refresh times."""
        from MTS_Management.cluster import load_sites
        site = site or load_sites(self.root / "MTSManager/sites.toml")[site_name]
        identity_settings = asdict(site)
        identity_settings.pop("manager_directory", None)
        identity_settings.pop("disabled", None)
        identity = hashlib.sha256(json.dumps(identity_settings, sort_keys=True).encode()).hexdigest()
        requested = set(config_hashes) if config_hashes is not None else None
        scope = hashlib.sha256(json.dumps(sorted(requested)).encode()).hexdigest() if requested is not None else "*"
        self.state.mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.state / "inventory.sqlite3")) as database, database:
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
        for path in sorted((self.root / "MTSManager/campaigns").rglob("simulations.json")):
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
        from MTS_Management.data import seed_group_fields
        from MTS_Management.files import file_digest, read_json
        from MTS_Management.inventory import progress, config_metadata_stamp
        from MTS_Management.run_records import read_run
        rows = []
        receipts = {}
        for manifest in sorted((self.root / "MTSManager/.state/retrieved").glob("*/*/manifest.json")):
            folder = manifest.parent
            if (folder.parent / ARCHIVE_MARKER).is_file():
                continue
            termination_path = folder / "termination.json"
            receipt_path = folder.parent / "snapshot.json"
            paths = [manifest, folder / "config.conf", folder / "macroData.csv", termination_path, receipt_path]
            stamp = tuple(p.stat().st_mtime_ns if p.exists() else None for p in paths) + (tuple(tuple(v) for v in config_metadata_stamp()),)
            if manifest not in self.local_files or self.local_files[manifest][0] != stamp:
                config, record = read_run(folder, allow_legacy_vtu=True)
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
        profiles = load_sites(self.root / "MTSManager/sites.toml")
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
                path = self.state / "configurations" / (row["config_hash"] + ".conf")
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
            root = (self.root / "MTSManager/.state/retrieved").resolve()
            valid = folder.parent.parent == root
        else:
            profile = load_sites(self.root / "MTSManager/sites.toml").get(site)
            valid = bool(profile and not profile.ssh and folder.parent == Path(paths(profile)["output_root"]))
        if not valid or not folder.is_dir():
            raise ValueError("Local simulation folder is unavailable. Refresh Monitor data.")
        if read_json(folder / "manifest.json")["config_hash"] != data["config_hash"]:
            raise ValueError("Local simulation identity changed. Refresh Monitor data.")
        command = ["/usr/bin/open", "-a", "Finder"] if sys.platform == "darwin" else ["xdg-open"]
        subprocess.run([*command, str(folder)], check=True, capture_output=True, text=True, timeout=10)
        return {"opened": True}

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

    def run_template(self, data):
        from MTS_Management.errors import ExpectedError
        from MTS_Management.run_template import read_template

        rows = self.selected_inventory_runs(data)
        if len(rows) != 1:
            raise ExpectedError("Select one simulation to use as a template.")
        row = rows[0]
        selection = {key: row[key] for key in ("folder", "config_hash", "manifest_sha256")}
        if row["site"] in {None, "local"}:
            result = read_template(row["folder"], selection)
        else:
            try:
                result = self.workspace.manager_call(row["site"], "run_template", selection=selection, timeout=60)
            except RuntimeError as error:
                if "Unknown remote operation: run_template" not in str(error):
                    raise
                raise ExpectedError(f"Update the manager on {site_label(row['site'])} in Setup and tools.") from error
        return result | {"site": row["site"] or row.get("origin_site") or "local"}

    def run_conflict(self, data):
        from MTS_Management.cluster import call, load_sites
        from MTS_Management.config import Config

        site = load_sites(self.root / "MTSManager/sites.toml")[data["site"]]
        config = simulation_setup.simulation_config(data)
        if checksum := data.get("initial_mesh_sha256"):
            config = Config(**(config.as_dict() | {"initialMeshSha256": checksum}))
        return call(site, "run_conflict", settings=config.as_dict(), label=data.get("label") or None, timeout=15)

    def remove_missing_runs(self, scope):
        from MTS_Management.cluster import load_sites
        from MTS_Management.errors import ExpectedError

        profiles = load_sites(self.root / "MTSManager/sites.toml")
        groups = {}
        for row in self.inventory_overview(**scope)["rows"]:
            if row.get("missing") and row["site"]:
                groups.setdefault(row["site"], []).append(
                    {key: row[key] for key in ("folder", "config_hash")})
        removed, errors = 0, {}
        for name, runs in groups.items():
            try:
                result = self.workspace.manager_call(name, "remove_missing", runs=runs)
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
                self.workspace.log(detail)
        if errors and not removed:
            raise ExpectedError(next(iter(errors.values())))
        self.workspace.log(f"Removed {removed} missing run records from the inventory.")
        return {"missing_removed": removed, "errors": errors}

    def read_inventory(self, name, profile, **options):
        try:
            return self.workspace.manager_call(name, "inventory", site=profile, timeout=60,
                                     include_archived=True, **options)
        except RuntimeError as error:
            if "unexpected keyword argument 'include_archived'" not in str(error):
                raise
            return self.workspace.manager_call(name, "inventory", site=profile, timeout=60, **options)

    def refresh_local_inventory(self, *, site, selection):
        from MTS_Management.cluster import call, load_sites
        from MTS_Management.errors import ExpectedError

        profiles = load_sites(self.root / "MTSManager/sites.toml")
        if site not in profiles or profiles[site].ssh:
            raise ExpectedError("Select an enabled local server for this refresh.")
        if "site" in selection and selection["site"] not in profiles:
            raise ExpectedError("The selected server is unavailable. Select a server again.")
        with self.workspace.lock:
            if self.workspace.stopping:
                raise ExpectedError("MTS is shutting down.")
            if self.workspace.task["state"] == "running" or not self.local_inventory_lock.acquire(blocking=False):
                raise ExpectedError("An operation is already running. Wait for it to finish.")
        try:
            for name, profile in profiles.items():
                if not profile.ssh:
                    rows = call(profile, "inventory", timeout=60, include_archived=True)
                    self.inventory_cache(name, rows=rows, site=profile)
            return self.inventory_overview(**selection)
        except ExpectedError:
            raise
        except Exception:
            traceback.print_exc()
            raise
        finally:
            self.local_inventory_lock.release()

    def archived_downloads(self):
        from MTS_Management.archive import ARCHIVE_MARKER
        from MTS_Management.files import read_json
        root = self.root / "MTSManager/.state/retrieved"
        return {read_json(p)["config_hash"] for marker in root.glob("*/" + ARCHIVE_MARKER)
                for p in marker.parent.glob("*/manifest.json")}

    def archive_history(self):
        from MTS_Management.cluster import load_sites, paths
        from MTS_Management.journal import Journal

        profiles = load_sites(self.root / "MTSManager/sites.toml", include_disabled=True)
        journal = Journal(self.state / "archive")
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
            root = self.root / "MTSManager/campaigns"
            with locked(self.state / ".simulation-campaigns.lock"):
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
            if original.parent.parent != (self.root / "MTSManager/.state/retrieved").resolve():
                raise ValueError("Invalid download archive paths")
            restore_simulation(original.parent, self.state, row, downloaded=True)
        else:
            profiles = load_sites(self.root / "MTSManager/sites.toml")
            run = {k: row.get(k) for k in ("config_hash", "manifest_sha256", "output_folder", "archive_folder")}
            try:
                result = self.workspace.manager_call(name, "restore", site=profiles[name], timeout=120, run=run)
            except RuntimeError as error:
                if "Unknown remote operation: restore" in str(error):
                    raise ExpectedError(f"Update the manager on {site_label(name)} in Setup and tools, then retry Restore.") from error
                raise
            if result.get("error"):
                raise ExpectedError(result["error"])
        Journal(self.state / "archive").append("restored", archive_id=archive_id)
        self.workspace.log(f"Restored: {original}")
        if name is not None:
            hashes = [row["config_hash"]]
            try:
                rows = self.read_inventory(name, profiles[name], config_hashes=hashes)
                self.inventory_cache(name, rows=rows, site=profiles[name], config_hashes=hashes)
            except (RuntimeError, ConnectionError, subprocess.SubprocessError) as error:
                self.workspace.log(f"Run restored. Refresh Monitor data to update its record. {error}")
        with self.workspace.lock:
            self.workspace.task["inventory_revision"] = 1
        return {"restored": str(original)}

    def archive_monitor(self, data):
        from MTS_Management.archive import archive_simulations
        from MTS_Management.cluster import load_sites
        from MTS_Management.errors import ExpectedError
        from MTS_Management.journal import Journal

        profiles = load_sites(self.root / "MTSManager/sites.toml")
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
                    if location.parent != (self.root / "MTSManager/.state/retrieved").resolve():
                        raise ValueError(f"Invalid download location: {location}")
                    folders = archive_simulations(location, self.state, runs, downloaded=True)
                else:
                    result = self.workspace.manager_call(name, "archive", site=profiles[name], timeout=120, runs=runs)
                    if result.get("error"):
                        raise ExpectedError(result["error"])
                    folders = result["folders"]
                moved.extend(folders)
                self.workspace.log("Archived: " + ", ".join(folders))
                selected = [row["config_hash"] for row in runs]
                hashes.update(selected)
                if name is None or profiles[name].ssh:
                    journal = Journal(self.state / "archive")
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
                self.workspace.log(str(error))
        if not moved:
            raise ExpectedError(next(iter(errors.values())))
        with self.workspace.lock:
            self.workspace.task["inventory_revision"] = 1
        campaign_notes = []
        for name, expected in self.campaign_definitions().items():
            if hashes & expected.keys():
                try:
                    self.archive_campaign(name, expected, automatic=True)
                except ExpectedError as error:
                    campaign_notes.append(str(error))
                    self.workspace.log(str(error))
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
        profiles = load_sites(self.root / "MTSManager/sites.toml", include_disabled=True)
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
                self.workspace.log(f"{site_label(site)} verification: {results[site]['error']}")
            raise ExpectedError(f"Campaign settings retained for {name}. Could not verify: {', '.join(unverified)}.")
        rows = [row for result in results.values() for row in result["data"]]
        for site, result in results.items():
            self.inventory_cache(site, rows=result["data"], site=profiles[site], config_hashes=list(expected))
        if any(not row.get("archived") for row in rows) or any(
                row["config_hash"] in expected for row in self.local_copies()):
            return False
        if not expected.keys() <= ({row["config_hash"] for row in rows} | self.archived_downloads()):
            return False
        root = self.root / "MTSManager/campaigns"
        with locked(self.state / ".simulation-campaigns.lock"):
            source = self.campaign_path(name).parent
            if source == root or any(p.parent != source for p in source.rglob("simulations.json")):
                raise ExpectedError(f"Campaign settings retained for {name}. The directory contains other campaign definitions.")
            destination = archive_root(root) / source.name
            identity = file_digest(source / "simulations.json")
            move_folder(source, destination)
            Journal(self.state / "archive").append("archived", kind="campaign", site=None, campaign=name,
                                              output_folder=str(source), archive_folder=str(destination),
                                              definition_sha256=identity)
            self.workspace.log(f"Archived campaign settings: {source} → {destination}")
        return True

    def refresh_inventory(self, action, data):
        from MTS_Management.cluster import load_sites
        profiles = load_sites(self.root / "MTSManager/sites.toml")
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
            self.workspace.check_cancelled()
            self.workspace.log(f"Updating {site_label(name)} ({index}/{len(names)})")
            try:
                options = {"hard_refresh": True} if action == "hard_refresh" else {}
                if config_hashes is not None:
                    options["config_hashes"] = config_hashes
                for attempt in range(1, 4):
                    self.workspace.check_cancelled()
                    self.workspace.log(f"Inventory attempt {attempt}/3; timeout 60 s")
                    try:
                        rows = self.read_inventory(name, profiles[name], **options)
                        break
                    except (subprocess.TimeoutExpired, TimeoutError, ConnectionError) as error:
                        self.workspace.check_cancelled()
                        if attempt == 3:
                            raise
                        self.workspace.log(f"{site_label(name)}: {error}; retrying.")
                self.workspace.check_cancelled()
                self.inventory_cache(name, rows=rows, site=profiles[name], config_hashes=config_hashes)
                updated.append(name)
                with self.workspace.lock:
                    self.workspace.task["inventory_revision"] = len(updated)
                self.workspace.log(f"Inventory updated: {site_label(name)}; {len(rows)} simulations saved locally.")
            except CancelledError:
                raise
            except Exception as error:
                if campaign is None and data.get("site") != "*":
                    raise
                errors[name] = str(error)
                detail = getattr(error, "stderr", None)
                self.workspace.log(f"{site_label(name)} failed: {error}" + (f"\n{detail}" if detail else ""))
        if not updated:
            raise ValueError("No server inventories were updated. Previous snapshots retained.")
        return {"inventory_updated": True, "updated_sites": updated, "errors": errors}
