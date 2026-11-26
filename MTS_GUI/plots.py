"""Local plot sources, previews, presets and animation exports."""

from concurrent.futures import CancelledError, ProcessPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict
import json
import multiprocessing
import os
from pathlib import Path
import re
import secrets
import shutil
from stat import S_ISREG
import subprocess
import sys
import tempfile
import threading
import time

from . import simulation_setup
from .install import run
from .monitor import site_label


class Plots:
    def __init__(self, workspace, root, state):
        self.workspace = workspace
        self.root = root
        self.state = state
        self.run_plot_sources = {}
        self.animation_memory = None
        self.preview_pool = None
        self.preview_lock = threading.Lock()
        self.preview_future = None
        self.preview_key = None
        self.preview_image = None

    def close(self):
        if self.preview_pool is not None:
            self.preview_pool.shutdown(wait=False, cancel_futures=True)

    def run_plot_source(self, selection):
        from MTS_Management.errors import ExpectedError

        if selection not in self.run_plot_sources:
            raise ExpectedError("Select the simulation again in Monitor data.")
        return self.run_plot_sources[selection]

    def restart_plot_sources(self, *, restore=False):
        path = self.state / "restart-plot-sources.json"
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
            self.workspace.history.append({"id": secrets.token_hex(8), "title": "Recover workspace", "state": "failed",
                                 "created_at": time.time() * 1000,
                                 "logs": [f"Monitor selections could not be restored. {error}"]})

    def open_run_plot(self, data):
        from MTS_Management.cluster import fetch_many, load_sites, paths
        from MTS_Management.data import plot_seed_group
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import digest, file_digest

        profiles = load_sites(self.root / "MTSManager/sites.toml")
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
                    for row in self.workspace.monitor.selected_inventory_runs(data)]
            anchor = rows[0]
            profile = profiles.get(anchor["site"])
            origin = Path(anchor["folder"])
            identity = {"folder": str(origin), "manifest_sha256": anchor["manifest_sha256"]}
            if profile and profile.ssh:
                try:
                    result = self.workspace.manager_call(profile.name, "plot_seed_group", site=profile, **identity)
                except RuntimeError as error:
                    if "Unknown remote operation: plot_seed_group" not in str(error):
                        raise
                    self.workspace.log(f"Update the manager on {site_label(profile.name)} to detect other seeds on the server.")
                    result = {"runs": rows}
                if result.get("error"):
                    raise ExpectedError(result["error"])
                group = result["runs"]
                collection = digest([asdict(profile), sorted(row["manifest_sha256"] for row in group)])
                local_run = self.state / "plot-downloads" / collection / origin.name
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
            self.workspace.log(f"Downloading macro data from {site_label(profile.name)} ({len(chosen)} runs).")
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
            return self.workspace.monitor.campaign_path(name), root, spec
        source = self.run_plot_source(name)
        if root and Path(root).resolve() != source["path"]:
            raise ValueError("Select the simulation again in Monitor data.")
        return source["path"], source["path"], spec | {"run_manifests": source["manifests"]}

    def animation_source(self, data):
        from MTS_Management.campaign import campaign_identity
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
            if seed != "all" and type(seed) is not int:
                raise ValueError("Select a saved seed or all seeds.")
            _, expected = campaign_identity(source)
            folders = matching_folders(Path(root).expanduser().resolve(), expected,
                                       campaign=name, seed=seed if seed != "all" else None)
            if len(folders) == 1:
                return folders[0]
        return None

    def default_animation_preview(self, campaign, folder):
        if (folder / "termination.json").is_file():
            return "middle"
        anchor = self.run_plot_sources.get(campaign, {}).get("anchor")
        if anchor and anchor["site"]:
            from MTS_Management.cluster import load_sites

            site = load_sites(self.root / "MTSManager/sites.toml").get(anchor["site"])
            if site is not None and any(
                    row["folder"] == anchor["folder"] and
                    row.get("manifest_sha256") == anchor["manifest_sha256"] and
                    row["status"]["state"] == "finished"
                    for row in self.workspace.monitor.inventory_cache(site.name, site=site)["rows"]):
                return "middle"
        return "last"

    def animation_options(self, data, *, fields=False):
        from MTS_Plotting.runs import Run

        folder = self.animation_source(data)
        if folder is None:
            return {}
        config = Run.read(folder).config
        if fields:
            from MTS_Management.data import mesh_snapshots, vtu_fields
            from MTS_Management.errors import ExpectedError

            source = self.run_plot_sources.get(data["campaign"])
            if source and source["site"]:
                result = self.remote_mesh_catalog(source, fields=True)
                if "fields" not in result:
                    raise ExpectedError("Use Deploy in SSH connections to enable VTU field selection on this server.")
                return result
            path = Path(data.get("data") or "").expanduser()
            if data["campaign"] == "@mesh" and path.suffix.lower() == ".vtu" and path.is_file():
                return {"fields": vtu_fields(path)}
            catalog = mesh_snapshots(folder)
            return {"fields": vtu_fields(folder / "data" / catalog[-1]["name"]) if catalog else {}}
        energy_function = config["energyFunction"]
        return {"boundary": config["boundary"], "energy_function": energy_function,
                "square_model": energy_function == "contiSquare",
                "delaunay_dislocations": config["reconnectionMethod"] == "none",
                "preview_frame": self.default_animation_preview(data["campaign"], folder)}

    def remote_mesh_catalog(self, source, *, site=None, fields=False):
        from MTS_Management.cluster import call, load_sites
        from MTS_Management.errors import ExpectedError

        site = site or load_sites(self.root / "MTSManager/sites.toml").get(source["site"])
        if site is None:
            raise ExpectedError("The server profile is unavailable. Select the simulation again.")
        anchor = source["anchor"]
        identity = {"folder": anchor["folder"], "manifest_sha256": anchor["manifest_sha256"]}
        try:
            if fields:
                result = call(site, "mesh_snapshots", **identity, fields=True)
            else:
                result = self.workspace.manager_call(site.name, "mesh_snapshots", site=site, **identity)
        except RuntimeError as error:
            if "Unknown remote operation: mesh_snapshots" in str(error):
                raise ExpectedError("Update the remote manager using Deploy in SSH connections to animate its mesh snapshots.") from error
            raise
        if result.get("error"):
            raise ExpectedError(result["error"])
        return result

    def render_animation(self, data, *, preview=False):
        from MTS_Management.cluster import fetch_many, load_sites
        from MTS_Management.data import mesh_snapshots
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import file_digest
        from MTS_Plotting.mesh_movie import (
            COLOUR_LIMITS_PROGRESS, colour_limit_options, display_limit_sources, display_scan_frames, frame_schedule,
            preview_frame_index, tiled_display,
        )
        from MTS_Plotting.runs import Run

        folder = self.animation_source(data)
        if folder is None:
            raise ExpectedError("Select one simulation to generate an animation.")
        selection = {"campaign": data["campaign"], "data": data["data"], "seed": data.get("seed", "all")}
        range_from_last_frame = data.get("range_from_last_frame", True)

        def log_animation(line):
            if line.startswith(COLOUR_LIMITS_PROGRESS):
                colour_limits = json.loads(line.removeprefix(COLOUR_LIMITS_PROGRESS))
                with self.workspace.lock:
                    self.workspace.task["animation_scan"] = {"animation_selection": selection, "colour_limits": colour_limits,
                                                   "range_options": range_options}
            rendering = re.fullmatch(r"Rendered \d+/\d+ mesh states\.", line)
            self.workspace.log(line, progress="animation-frames" if rendering else None)

        override = self.state / "ffmpeg-path"
        encoder = override.read_text().strip() if not preview and override.is_file() else None
        spec = {k: data[k] for k in ("kind", "matrix", "tiling", "playback", "fps", "seconds_per_load", "minimum_seconds", "limit")}
        spec["range_from_last_frame"] = range_from_last_frame
        spec.update({key: data[key] for key in ("dpi", "heatmap", "node_colours", "node_size", "show_nodes",
                                                "show_mesh_lines", "workers", "legend_size", "show_title",
                                                "colourbar_position", "colourbar_size",
                                                "title_size", "show_border", "show_legend", "dislocations",
                                                "dislocation_lattice", "dislocation_method",
                                                "dislocation_bond_tolerance", "dislocation_scale",
                                                "dislocation_thickness", "energy_min", "stress_component", "opacity",
                                                "opacity_curve", "vtu_field", "symmetric", "colour_min") if key in data})
        range_options = {key: spec[key] for key in ("kind", "matrix", "playback", "fps", "seconds_per_load",
                                                    "minimum_seconds", "range_from_last_frame")}
        range_options["stress_component"] = spec.get("stress_component", "12")
        range_options["symmetric"] = colour_limit_options(spec)["symmetric"]
        if spec["kind"] == "vtu":
            range_options["vtu_field"] = spec.get("vtu_field")
        rendering = {k: v for k, v in spec.items() if k != "workers"}
        source = self.run_plot_sources.get(data["campaign"])
        output = (self.state / "previews" / f"animation-{secrets.token_hex(8)}.png" if preview else
                  self.state / "videos" / secrets.token_hex(8) / "mesh.mp4")
        identity = file_digest(folder / "manifest.json")
        remote = bool(source and source["site"])
        with tempfile.TemporaryDirectory(dir=self.state, prefix="animation-") as temporary:
            staging = Path(temporary)
            staged_output = staging / "rendered" / output.name
            if remote:
                site = load_sites(self.root / "MTSManager/sites.toml").get(source["site"])
                if site is None:
                    raise ExpectedError("The server profile is unavailable. Select the simulation again.")
                anchor = source["anchor"]
                catalog = self.remote_mesh_catalog(source, site=site)["frames"]
            else:
                catalog = mesh_snapshots(folder)
            if not catalog:
                raise ExpectedError("No mesh snapshots are available. Choose a run with saved mesh output.")
            first = catalog[0]["name"]
            if remote:
                first_revision = None
            else:
                info = (folder / "data" / first).stat()
                first_revision = (info.st_mtime_ns, info.st_size)
            memory_key = (str(folder), identity, first, first_revision, rendering)
            if not preview and self.animation_memory and self.animation_memory[0] == memory_key:
                spec["frame_memory"] = self.animation_memory[1]
            spec.update(preview=preview, frames=catalog)
            if preview:
                spec["preview_frame"] = data.get("preview_frame") or self.default_animation_preview(data["campaign"], folder)
            if remote:
                indices = frame_schedule(
                    catalog, playback=spec["playback"], fps=spec["fps"],
                    seconds_per_load=spec["seconds_per_load"], minimum_seconds=spec["minimum_seconds"])
                scheduled = [catalog[index] for index in sorted(set(indices))]
                frames = [frame["name"] for frame in scheduled]
                identities = {anchor["config_hash"]: anchor["manifest_sha256"]}

                def download_frames(names):
                    self.workspace.log(f"Downloading {len(names)} mesh states from {site_label(site.name)}.")
                    fetched, = fetch_many(site, identities, staging / "snapshots", expected_manifests=identities,
                                          retain_binaries=False, mesh_frames=names, cancel=self.workspace.cancel)
                    return fetched

                if preview:
                    chosen = catalog[preview_frame_index(indices, spec["preview_frame"])]["name"]
                    kind = spec["kind"]
                    limits = {**colour_limit_options(spec),
                              "tiled": tiled_display(Run.read(folder).config, kind, spec["tiling"]),
                              "range_from_last_frame": range_from_last_frame}
                    _, _, scans = display_limit_sources(folder, scheduled, **limits)
                    _, scan_min, scan_max, scan_signed = scans
                    # The first transfer includes the preview, required VTU colour data and the current CSV.
                    required = (False, scan_min, scan_max and (kind != "energy" or range_from_last_frame), scan_signed)
                    downloaded = dict.fromkeys([chosen] + [frame["name"] for frame, _ in display_scan_frames(
                        scheduled, required, range_from_last_frame=range_from_last_frame)])
                    folder = download_frames(list(downloaded))
                    _, _, scans = display_limit_sources(folder, scheduled, **limits)
                    frames = [frame["name"] for frame, _ in display_scan_frames(
                        scheduled, scans, range_from_last_frame=range_from_last_frame)
                        if frame["name"] not in downloaded]
                if frames:
                    folder = download_frames(frames)
            options = staging / "options.json"
            options.write_text(json.dumps(spec))
            env = os.environ | {"MPLBACKEND": "Agg"}
            if encoder:
                env["MTS_FFMPEG"] = encoder
            result = run([sys.executable, "-m", "MTS_Plotting.mesh_movie", str(folder), str(staged_output), str(options)],
                         log_animation, json_result=True, env=env, cancel=self.workspace.cancel)
            staged, published = (staged_output, output) if preview else (staged_output.parent, output.parent)
            with self.workspace.lock:
                if self.workspace.cancel.is_set():
                    raise CancelledError("Animation aborted.")
                published.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(published)
                self.workspace.task["cancellable"] = False
        if preview:
            if spec["preview_frame"] == "first":
                self.animation_memory = (memory_key, result["frame_memory"])
            result = {"animation_preview": output.name, "energy_range": result.get("energy_range")}
        else:
            result["video"] = str(output.relative_to(self.state / "videos"))
        return result | {"animation_selection": selection}

    def plot_data_roots(self):
        from MTS_Management.cluster import load_sites, paths
        profiles = load_sites(self.root / "MTSManager/sites.toml")
        sources = [paths(profile)["output_root"] for profile in profiles.values() if not profile.ssh]
        sources += list((self.root / "MTSPlot/data").glob("*"))
        sources += list((self.root / "MTSManager/.state/retrieved").glob("*"))
        return sources

    def campaign_downloads(self, campaign):
        from MTS_Management.files import digest

        return self.state / "campaign-downloads" / digest(campaign)[:20]

    def campaign_csv_sources(self, campaign):
        """Use the local snapshots to identify remote CSVs for the saved campaign."""
        from MTS_Management.campaign import campaign_member
        from MTS_Management.cluster import load_sites
        from MTS_Management.errors import ExpectedError

        expected = self.workspace.monitor.campaign_definitions().get(campaign)
        if expected is None:
            raise ExpectedError("Select a registered campaign.")
        profiles = load_sites(self.root / "MTSManager/sites.toml")
        sources = {}
        for name, site in profiles.items():
            if not site.ssh:
                continue
            for row in self.workspace.monitor.inventory_cache(name, site=site)["rows"]:
                identity = row["config_hash"]
                if (campaign_member(row, campaign, expected) and not row.get("archived") and not row.get("missing")
                        and row["progress"].get("csv_modified") is not None):
                    sources.setdefault(identity, []).append((name, row))
        return profiles, sources

    @staticmethod
    def campaign_csv_source(destination, sources):
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import file_digest

        if len({row["manifest_sha256"] for _, row in sources}) > 1:
            matching = []
            for name, row in sources:
                manifest = destination / Path(row["folder"]).name / "manifest.json"
                if manifest.is_file() and file_digest(manifest) == row["manifest_sha256"]:
                    matching.append((name, row))
            if len({row["manifest_sha256"] for _, row in matching}) != 1:
                identity = sources[0][1]["config_hash"]
                raise ExpectedError(f"Multiple remote runs match {identity[:20]}. Choose a local collection "
                                    "that contains the intended run, or use Plot for that run in Monitor data.")
            sources = matching
        return max(sources, key=lambda item: item[1]["progress"]["csv_modified"])

    @staticmethod
    def campaign_csv_current(folder, row):
        from MTS_Management.files import file_digest

        try:
            stat = (folder / "macroData.csv").stat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        observed = row["progress"]
        modified = observed["csv_modified"]
        # Some rsync versions preserve whole seconds only. Use size to detect
        # appends within that second when the inventory includes it.
        if not S_ISREG(stat.st_mode) or int(stat.st_mtime) < int(modified):
            return False
        if int(stat.st_mtime) == int(modified) and (
                (stat.st_mtime_ns % 1_000_000_000 != 0 and stat.st_mtime < modified)
                or stat.st_size < observed.get("csv_size", 0)):
            return False
        manifest = folder / "manifest.json"
        return (manifest.is_file() and file_digest(manifest) == row["manifest_sha256"]
                and (not row.get("termination") or (folder / "termination.json").is_file()))

    def campaign_csv_outdated(self, campaign, data):
        from MTS_Management.errors import ExpectedError

        destination = Path(data).expanduser().resolve() if data else self.campaign_downloads(campaign)
        _, sources = self.campaign_csv_sources(campaign)
        single_run = (destination / "manifest.json").is_file()
        try:
            for group in sources.values():
                _, row = self.campaign_csv_source(destination, group)
                folder = destination if single_run else destination / Path(row["folder"]).name
                if not self.campaign_csv_current(folder, row):
                    return True
        except ExpectedError:
            return True
        return False

    def refresh_campaign_csvs(self, data):
        from MTS_Management.cluster import fetch_many, paths
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import digest, read_json

        campaign, requested_data = data["campaign"], data.get("data")
        profiles, sources = self.campaign_csv_sources(campaign)
        destination = Path(requested_data).expanduser().resolve() if requested_data else self.campaign_downloads(campaign)
        if (destination.is_file() or (destination / "manifest.json").exists()
                or any(destination.is_relative_to(Path(paths(site)["output_root"]))
                       for site in profiles.values() if not site.ssh)):
            destination = self.campaign_downloads(campaign)
        batches = {}
        for identity, group in sources.items():
            name, row = self.campaign_csv_source(destination, group)
            if not self.campaign_csv_current(destination / Path(row["folder"]).name, row):
                batches.setdefault(name, {})[identity] = row
        selected = {identity: row for rows in batches.values() for identity, row in rows.items()}
        if selected:
            for manifest in destination.glob("*/manifest.json"):
                row = selected.get(read_json(manifest)["config_hash"])
                if row is not None and manifest.parent.name != Path(row["folder"]).name:
                    raise ExpectedError(f"Another run of the same configuration is in {manifest.parent}. "
                                        "Choose a separate local collection for the download.")
        for row in selected.values():
            folder = destination / Path(row["folder"]).name
            if folder.is_symlink() or any((folder / name).is_symlink() for name in
                                          ("config.conf", "manifest.json", "macroData.csv", "termination.json")):
                raise ExpectedError(f"{folder} contains a symbolic link. Choose a separate local collection.")

        errors, downloaded = {}, 0
        for name, rows in batches.items():
            self.workspace.check_cancelled()
            label = site_label(name)
            self.workspace.log(f"{label}: Downloading campaign data for {len(rows):,} simulations.")
            manifests = {identity: row["manifest_sha256"] for identity, row in rows.items()}
            receipt = self.state / "campaign-retrievals" / (digest([str(destination), name]) + ".json")
            try:
                fetched = fetch_many(profiles[name], manifests, destination, expected_manifests=manifests,
                                     retain_binaries=False, cancel=self.workspace.cancel, inventory_timeout=300,
                                     receipt_path=receipt, force_transfer=True)
            except (ConnectionError, subprocess.SubprocessError, ExpectedError) as error:
                errors[name] = str(error)
                self.workspace.log(f"{label}: {error}")
            else:
                downloaded += len(fetched)
        if errors and not downloaded:
            raise ExpectedError("Campaign data could not be downloaded. See the terminal for details.")
        return {"campaign_csv_refresh": {"campaign": campaign, "requested_data": requested_data,
                                         "data": str(destination), "downloaded": downloaded}, "errors": errors}

    @staticmethod
    def local_data_file(value, extensions):
        path = Path(value or "").expanduser()
        if not path.is_absolute() or path.suffix.lower() not in extensions or not path.is_file():
            raise ValueError(f"File unavailable at {value!r}. Enter its full path.")
        return path.resolve()

    def mesh_file_run(self, value):
        path = Path(value or "").expanduser()
        if path.is_absolute() and path.is_dir():
            folders = (path.resolve(),)
        else:
            path = self.local_data_file(value, {".pvd", ".vtu"})
            folders = (path.parent, path.parent.parent)
        for folder in folders:
            if all((folder / name).is_file() for name in ("config.conf", "manifest.json")):
                return folder
        missing = [name for name in ("config.conf", "manifest.json") if not (folders[-1] / name).is_file()]
        raise ValueError(f"Animation requires a managed run. Missing {', '.join(missing)} for {value!r}.")

    def open_simulation_folder(self, data):
        value, action = data["path"], data["action"]
        path = value.strip()
        if len(path) >= 2 and path[0] in {"'", '"'} and path[-1] == path[0]:
            path = path[1:-1]
        folder = Path(path).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError(f"Folder unavailable at {value!r}. Choose or enter the full path to a simulation folder.")
        folder = folder.resolve()
        if action == "plot":
            csv = folder / "macroData.csv"
            if not csv.is_file():
                raise ValueError(f"Plot requires macroData.csv in {str(folder)!r}.")
            return {"plot_selection": "@csv", "plot_name": folder.name, "path": str(csv)}
        if action == "settings":
            config = folder / "config.conf"
            if not config.is_file():
                raise ValueError(f"Load settings requires config.conf in {str(folder)!r}.")
            try:
                return simulation_setup.preset(self.state / "simulation-presets",
                                               {"action": "import", "text": config.read_text()})
            except ValueError as error:
                raise ValueError(f"Could not load {str(config)!r}. {error}") from error
        if action == "animate":
            from MTS_Management.data import mesh_snapshots
            from MTS_Plotting.runs import Run

            self.mesh_file_run(str(folder))
            Run.read(folder)
            if not mesh_snapshots(folder):
                raise ValueError(f"Animate requires native VTU snapshots in {str(folder / 'data')!r}.")
            return {"plot_selection": "@mesh", "plot_name": folder.name, "path": str(folder)}
        raise ValueError(f"Unknown folder action {action!r}.")

    @staticmethod
    def pick_simulation_folder():
        if sys.platform == "darwin":
            command = ["/usr/bin/osascript", "-e",
                       'POSIX path of (choose folder with prompt "Choose an MTS simulation folder")']
        elif zenity := shutil.which("zenity"):
            command = [zenity, "--file-selection", "--directory", "--title=Choose an MTS simulation folder"]
        elif kdialog := shutil.which("kdialog"):
            command = [kdialog, "--getexistingdirectory", str(Path.home()), "--title", "Choose an MTS simulation folder"]
        else:
            raise ValueError("Enter the folder path directly or install Zenity or KDialog to browse.")
        result = subprocess.run(command, text=True, capture_output=True)
        if not result.returncode:
            return {"path": result.stdout.removesuffix("\n")}
        if "(-128)" in result.stderr or result.returncode == 1 and not result.stderr:
            return {"path": None}
        raise RuntimeError(f"Could not open the folder picker. Paste the folder path. {result.stderr.strip()}")

    def plot_options(self, campaign, data=None):
        from MTS_Plotting.campaign_plot import describe

        source, root, spec = self.plot_inputs({"campaign": campaign, "data": data})
        if "run_manifests" in spec or campaign == "@csv":
            schema = describe(source, [], root, run_manifests=spec.get("run_manifests"))
            return schema | {"data": str(Path(data).expanduser().resolve())} if campaign == "@mesh" else schema
        sources = self.plot_data_roots()
        managed = self.campaign_downloads(campaign)
        if managed.is_dir():
            sources.append(managed)
            root = root or managed
        return describe(source, sources, root)

    def plot_presets(self, data=None):
        return self.presets(data, kind="plot")

    def presets(self, data=None, *, kind):
        from MTS_Management.files import locked, read_json, write_json, now
        path = self.state / f"{kind}-presets.json"
        with locked(self.state / f".{kind}-presets.lock"):
            saved = read_json(path) if path.exists() else {"version": 1, "presets": []}
            if saved.keys() != {"version", "presets"} or saved["version"] != 1 or not isinstance(saved["presets"], list):
                raise ValueError(f"Invalid saved {kind} presets.")
            if data is not None:
                name, selection = data["name"].strip(), data["selection"]
                if not name or len(name) > 120 or not isinstance(selection, dict):
                    raise ValueError("A preset requires a name of 1–120 characters and settings.")
                if kind == "plot":
                    self.workspace.monitor.campaign_path(selection["campaign"])
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
                directory = self.state / "previews"
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

    def export_plot(self, data):
        # Matplotlib runs in its own process, isolated from HTTP request threads.
        folder = self.state / "figures" / secrets.token_hex(6)
        path = folder / "campaign-plot.pdf"
        self.workspace.log("Reading simulation tables and creating the figure…")
        source, root, spec = self.plot_inputs(data)
        result = run([sys.executable, "-m", "MTS_Plotting.campaign_plot", str(source),
                      str(Path(root).expanduser().resolve()), str(path), json.dumps(spec)],
                     self.workspace.log, json_result=True)
        for warning in result["warnings"]:
            self.workspace.log(warning)
        return result | {"pdf": str(path.relative_to(self.state / "figures")), "name": path.name}
