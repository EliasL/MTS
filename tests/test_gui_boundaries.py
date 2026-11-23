"""Protect privileged local operations and existing scientific source checkouts."""

import json
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from MTS_GUI.install import run, synchronize_components
from MTS_GUI.__main__ import check_server_lock
from MTS_GUI.monitor import Monitor
from MTS_GUI.server import Handler, ThreadingHTTPServer, Workspace


class Boundaries(unittest.TestCase):
    def test_animation_abort_stops_encoder_and_discards_only_the_current_export(self):
        encoder = """
import signal, sys, time
from pathlib import Path

def stop(*_):
    Path(sys.argv[1]).write_text('stopped')
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
print('Encoder ready', file=sys.stderr, flush=True)
time.sleep(10)
"""
        renderer = """
import subprocess, sys
from pathlib import Path

partial = Path(sys.argv[1]).with_suffix('.partial.mp4')
partial.parent.mkdir()
partial.write_bytes(b'incomplete')
subprocess.run([sys.executable, '-c', sys.argv[2], sys.argv[3]], check=True)
"""
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            source = state / "run"
            (source / "data").mkdir(parents=True)
            (source / "manifest.json").write_text("{}")
            (source / "data" / "mesh_load=0_nrM=0_.0.vtu").touch()
            previous = state / "videos" / "previous" / "mesh.mp4"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b'previous video')
            stopped = state / "encoder-stopped"
            ready = threading.Event()

            def render(command, log, **options):
                def progress(line):
                    if line == "Encoder ready":
                        ready.set()
                    if not line.startswith("$ "):
                        log(line)

                return run([sys.executable, "-c", renderer, command[-2], encoder, str(stopped)], progress, **options)

            settings = dict(campaign="Example", data=str(source), kind="energy", matrix="T", tiling="cell",
                            playback="saved", fps=1, seconds_per_load=1, minimum_seconds=1, limit=None)
            workspace = Workspace()
            with patch.object(workspace.plots, "state", state), \
                    patch.object(workspace.plots, "animation_source", return_value=source), \
                    patch("MTS_GUI.plots.run", side_effect=render):
                task_id = workspace.start("Generate animation", lambda: workspace.plots.render_animation(settings),
                                          cancellable=True, action="animate")
                self.assertTrue(ready.wait(5), workspace.task["logs"])
                workspace.abort(task_id)
                deadline = time.monotonic() + 4
                while workspace.task["state"] == "running" and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(workspace.task["state"], "aborted", workspace.task["logs"])
            self.assertEqual(stopped.read_text(), "stopped")
            self.assertEqual(list((state / "videos").glob("*/*.mp4")), [previous])
            self.assertEqual(previous.read_bytes(), b'previous video')
            self.assertEqual(list(state.glob("animation-*")), [])

    def test_local_build_status_distinguishes_outdated_from_missing(self):
        from MTS_GUI.server import local_build_status
        from MTS_Management.config import DEFAULTS
        from MTS_Management.files import digest, write_json

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / "MTS2D"
            source = root / "source"
            source.mkdir()
            paths = {"source": source, "executable": executable}
            write_json(source / "SOURCE.json", {"revision": "current"})
            self.assertEqual(local_build_status(paths), ("missing", False))
            executable.touch()
            manifest = root / "build-manifest.json"
            record = {"revision": "previous", "configuration_keys_sha256": digest(sorted(DEFAULTS))}
            write_json(manifest, record)
            self.assertEqual(local_build_status(paths), ("outdated", True))
            write_json(manifest, record | {"revision": "current"})
            self.assertEqual(local_build_status(paths), ("installed", True))
            write_json(manifest, {"revision": "current",
                                  "configuration_keys_sha256": digest(sorted(
                                      DEFAULTS.keys() - {"constitutiveReference", "vtuFilesPerUnitLoad"}
                                      | {"nrVTUFrames"}))})
            self.assertEqual(local_build_status(paths), ("outdated", False))

    def test_download_receipt_identifies_remote_folder_without_attempt_files(self):
        import shlex
        from MTS_Management.cluster import Site
        from MTS_Management.config import Config
        from MTS_Management.files import file_digest, write_json

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Config()
            folder = root / "MTSManager/.state/retrieved/macros" / config.folder_name()
            folder.mkdir(parents=True)
            config.write(folder / "config.conf")
            manifest = folder / "manifest.json"
            write_json(manifest, {"config_hash": config.config_hash, "settings": config.as_dict(),
                                  "created_at": "2026-11-10T10:00:00+01:00"})
            remote_root = "/work/PhD's results"
            write_json(folder.parent / "snapshot.json", {
                "site": "cluster", "source_root": remote_root, "runs": [
                    {"config_hash": config.config_hash, "manifest_sha256": file_digest(manifest)},
                ],
            })
            sites = {"local": Site(name="local"), "cluster": Site(name="cluster", ssh="my-cluster")}
            with patch("MTS_GUI.server.ROOT", root), patch("MTS_GUI.server.STATE", root / ".mts"), \
                    patch("MTS_Management.cluster.load_sites", return_value=sites):
                workspace = Workspace()
                row, = workspace.monitor.inventory_overview(site="local")["rows"]
                self.assertTrue(row["local_copy"])
                self.assertEqual(Path(row["folder"]).resolve(), folder.resolve())
                self.assertEqual(row["run_date"], "2026-11-10T10:00:00+01:00")
                ssh, terminal, separator, host, command = shlex.split(row["ssh_command"])
                self.assertEqual((ssh, terminal, separator, host), ("ssh", "-t", "--", "my-cluster"))
                self.assertEqual(shlex.split(command)[:3], ["cd", "--", f"{remote_root}/{folder.name}"])
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    workspace.monitor.open_run_folder({"folder": str(root), "config_hash": config.config_hash})

    def test_native_preset_expands_campaign_with_complete_slip_sequences(self):
        from itertools import product
        from MTS_GUI.simulation_setup import preset
        from MTS_Management.campaign import campaign_entries

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".mts"
            presets = state / "simulation-presets"
            imported = preset(presets, {"action": "import", "text": """name = Example
rows = 100
cols = 100
deformation = sequentialSlip
boundary = anchored
directions = -1.1,2.1
"""})
            saved = preset(presets, {"action": "save", "name": "Base", "settings": imported["settings"]})
            self.assertEqual(preset(presets, {"action": "import", "text": saved["text"]}), imported | {"label": "Base"})
            with patch("MTS_GUI.server.ROOT", root), patch("MTS_GUI.server.STATE", state):
                workspace = Workspace()
                workspace.save_campaign({"name": "Sweep", "preset": "Base", "writeMeshVTUs": False, "axes": [
                    {"parameter": "seed", "mode": "linear", "start": "3", "stop": "5", "count": "3"},
                    {"parameter": "epsR", "mode": "log", "start": "1e-7", "stop": "1e-5", "count": "3"},
                    {"parameter": "directions", "mode": "custom", "values": "-1.1,2.1\n-1.1,2.1,2.5"},
                ]})
                name, runs = campaign_entries(workspace.monitor.campaign_path("Sweep"))
                loaded = workspace.load_campaign(name)
                self.assertFalse(loaded["preserve_runs"])
                modes = {axis["parameter"]: axis["mode"] for axis in loaded["axes"]}
                self.assertEqual(modes, {"seed": "linear", "epsR": "log", "directions": "custom"})
                metadata = json.loads(workspace.monitor.campaign_path(name).with_name("metadata.json").read_text())
                self.assertEqual(metadata["sweep"]["axes"], loaded["axes"])
                self.assertEqual(workspace.campaign_size({"axes": loaded["axes"]}), {"jobs": 18})
                workspace.save_campaign({"name": "SweepCopy", "source_campaign": name,
                                         "base": {"settings": loaded["settings"]}, "axes": loaded["axes"]})
                _, copied = campaign_entries(workspace.monitor.campaign_path("SweepCopy"))
            self.assertEqual(name, "Sweep")
            self.assertEqual(len(runs), 18)
            self.assertEqual({c.config_hash for c, _ in copied}, {c.config_hash for c, _ in runs})
            self.assertTrue(preset(presets, {"action": "load", "name": "Base"})["settings"]["writeMeshVTUs"])
            self.assertFalse(any(config["writeMeshVTUs"] for config, _ in runs))
            self.assertEqual(
                {(c["seed"], float(f"{c['epsR']:.12g}"), (c["directions"], c["totalLoad"])) for c, _ in runs},
                set(product([3, 4, 5], [1e-7, 1e-6, 1e-5], [("-1.1,2.1", 2), ("-1.1,2.1,2.5", 3)])),
            )

    def test_load_campaign_preserves_sparse_members_when_editing_checkpoint_settings(self):
        from MTS_Management.campaign import campaign_entries
        from MTS_Management.config import Config
        from MTS_Management.files import read_json, write_json

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "MTSManager/campaigns/benchmark/simulations.json"
            members = [(Config(rows=50, cols=50, nrThreads=2, writeDumps=False,
                               logDuringMinimization=True), "Diagnostic"),
                       (Config(rows=50, cols=50, nrThreads=2, writeDumps=False), "Baseline"),
                       (Config(rows=100, cols=100, nrThreads=1, writeDumps=False), "L100")]
            definition = {"name": "Benchmark", "runs": [
                {"label": label, "settings": config.as_dict()} for config, label in members]}
            resources = {"site": "magi", "resources": {"100": {"time": "3-00:00:00"}},
                         "diagnostic_label": "Diagnostic"}
            write_json(source, definition)
            write_json(source.with_name("resources.json"), resources | {"campaign_sha256": "original"})
            with patch("MTS_GUI.server.ROOT", root), patch("MTS_GUI.server.STATE", root / ".mts"):
                workspace = Workspace()
                self.assertIn("Benchmark", workspace.start_options()["campaign_sources"])
                loaded = workspace.load_campaign("Benchmark")
                self.assertTrue(loaded["preserve_runs"])
                self.assertEqual((loaded["label"], loaded["site"]), ("Baseline", "magi"))
                self.assertEqual({axis["parameter"] for axis in loaded["axes"]}, {"L", "nrThreads"})
                selection = {"source_campaign": loaded["name"], "preserve_runs": True}
                self.assertEqual(workspace.campaign_size(selection | {"axes": loaded["axes"]}), {"jobs": 2})
                workspace.save_campaign({"name": "Recovery", "source_campaign": loaded["name"], "site": loaded["site"],
                                         "axes": loaded["axes"], "preserve_runs": True, "base": {
                                             "settings": loaded["settings"] | {"writeDumps": True, "checkpointInterval": 50}}})
                target = workspace.monitor.campaign_path("Recovery")
                _, copied = campaign_entries(target)
                filtered_axes = [{"parameter": "L", "mode": "custom", "values": "100"},
                                 {"parameter": "nrThreads", "mode": "custom", "values": "1,2"}]
                self.assertEqual(workspace.campaign_size(selection | {"axes": filtered_axes}), {"jobs": 1})
                self.assertEqual(workspace.campaign_size(selection | {"axes": [
                    filtered_axes[0], {"parameter": "nrThreads", "mode": "custom", "values": "2"}]}), {"jobs": 0})
                workspace.save_campaign({"name": "L100Only", "source_campaign": loaded["name"], "preserve_runs": True,
                                         "base": {"settings": loaded["settings"]}, "axes": filtered_axes})
                _, filtered = campaign_entries(workspace.monitor.campaign_path("L100Only"))
                reopened = workspace.load_campaign("L100Only")
                self.assertEqual(workspace.campaign_size({"source_campaign": reopened["name"], "preserve_runs": True,
                                                         "axes": reopened["axes"]}), {"jobs": 1})
            self.assertEqual([(config.as_dict(), label) for config, label in copied], [
                (config.as_dict() | {"writeDumps": True, "checkpointInterval": 50}, label)
                for config, label in members[1:]])
            self.assertEqual([(config.config_hash, label) for config, label in filtered],
                             [(members[-1][0].config_hash, "L100")])
            self.assertEqual(read_json(target.with_name("resources.json")),
                             {"site": resources["site"], "resources": resources["resources"]})
            self.assertEqual(read_json(source), definition)

    def test_legacy_preset_load_returns_current_settings_and_downloadable_configuration(self):
        from MTS_GUI.simulation_setup import preset
        from MTS_Management.config import Config
        from MTS_Management.files import digest

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            settings = Config(totalLoad=2, writeMeshVTUs=True).as_dict()
            settings.pop("vtuFilesPerUnitLoad")
            settings["nrVTUFrames"] = 500
            identity = digest({"format": "mtscripts-config-v1", "settings": settings})
            path = folder / "Legacy.conf"
            text = f"name = Legacy[{identity[:20]}]\n" + "\n".join(
                f"{key} = {value}" for key, value in settings.items())
            path.write_text(text)

            with self.assertRaisesRegex(ValueError, "Unknown setting: nrVTUFrames"):
                Config.read(path)
            loaded = preset(folder, {"action": "load", "name": "Legacy"})
            self.assertEqual(loaded["settings"]["vtuFilesPerUnitLoad"], 250)
            self.assertEqual(preset(folder, {"action": "import", "text": loaded["text"]})["settings"],
                             loaded["settings"])
            self.assertEqual(path.read_text(), text)

    def test_rectangle_preset_keeps_driven_edge_and_constraints(self):
        from MTS_GUI.simulation_setup import preset

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            imported = preset(folder, {"action": "import", "text": """deformation = compression
boundary = rectangle
drivenEdge = top
top = fixed_y
bottom = fixed
rows = 30
cols = 10
totalLoad = 3
"""})
            saved = preset(folder, {"action": "save", "name": "Compression", "settings": imported["settings"]})
            loaded = preset(folder, {"action": "load", "name": "Compression"})
            self.assertEqual(loaded, saved)
            self.assertEqual((loaded["settings"]["drivenEdge"], loaded["settings"]["top"],
                              loaded["settings"]["bottom"]), ("top", "fixed_y", "fixed"))
            from MTS_Management.config import Config
            from MTS_Management.errors import ExpectedError

            with self.assertRaisesRegex(ExpectedError, "Set top to fixed_y or fixed"):
                Config(**(imported["settings"] | {"top": "free"}))
            legacy = Config.saved(imported["settings"] | {"top": "fixed_x"})
            self.assertEqual((legacy["top"], legacy.as_dict()["top"]), ("fixed", "fixed_x"))

    def test_plot_presets_require_explicit_update_and_persist_independent_copies(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("MTS_GUI.server.STATE", Path(temporary)), \
                patch.object(Monitor, "campaign_path", return_value=Path("campaign.json")):
            workspace = Workspace()
            selection = {"campaign": "Example", "x": "load", "y": "avg_energy", "xscale": "linear"}
            first = workspace.plots.plot_presets({"name": "Energy", "selection": selection})
            first_id = first["saved_id"]
            with self.assertRaisesRegex(ValueError, "already exists"):
                workspace.plots.plot_presets({"name": "Energy", "selection": selection | {"xscale": "log"}})
            second = workspace.plots.plot_presets({"name": "Energy logarithmic", "selection": selection | {"xscale": "log"}})
            workspace.plots.plot_presets({"id": first_id, "name": "Stress", "selection": selection | {"y": "avg_sigma12"}})
            saved = {p["id"]: p for p in Workspace().plots.plot_presets()["presets"]}
            self.assertEqual(saved[first_id]["selection"]["y"], "avg_sigma12")
            self.assertEqual(saved[second["saved_id"]]["selection"], selection | {"xscale": "log"})

    def test_folder_plot_reads_csv_without_requiring_run_metadata(self):
        from MTS_Plotting.campaign_plot import prepared_plot

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve() / "run's output"
            folder.mkdir()
            csv = folder / "macroData.csv"
            csv.write_text("time,value,tag\n2,3,a\n0,5,b\n")
            workspace = Workspace()
            selected = workspace.plots.open_simulation_folder({"path": str(folder), "action": "plot"})
            for quote in ("'", '"'):
                with self.subTest(quote=quote):
                    quoted = workspace.plots.open_simulation_folder({"path": f" {quote}{folder}{quote} ", "action": "plot"})
                    self.assertEqual(quoted["path"], str(csv))
            spec = {"campaign": selected["plot_selection"], "data": selected["path"], "x": "time", "y": "value"}
            schema, result = prepared_plot(*workspace.plots.plot_inputs(spec))
            self.assertEqual(schema["data"], str(csv))
            self.assertEqual(set(schema["quantities"]), {"time", "value"})
            item, = result["series"]
            self.assertEqual((item["x"], item["mean"]), ([2, 0], [3, 5]))

    def test_folder_animation_selects_full_run(self):
        from MTS_Management.config import Config
        from MTS_Management.data import mesh_snapshots

        with tempfile.TemporaryDirectory() as temporary:
            config = Config()
            folder = Path(temporary).resolve() / config.folder_name()
            mesh = folder / "data" / "example_load=1_nrM=0_.2.vtu"
            mesh.parent.mkdir(parents=True)
            mesh.touch()
            mesh.with_name("example_load=0_nrM=0_.1.vtu").touch()
            config.write(folder / "config.conf")
            (folder / "manifest.json").write_text(json.dumps({
                "settings": config.as_dict(), "config_hash": config.config_hash, "executable_sha256": "fixture"}))
            workspace = Workspace()
            selected = workspace.plots.open_simulation_folder({"path": str(folder), "action": "animate"})
            spec = {"campaign": selected["plot_selection"], "data": selected["path"]}
            run = workspace.plots.animation_source(spec)
            self.assertEqual(run, folder)
            self.assertEqual([frame["step"] for frame in mesh_snapshots(run)], [1, 2])
            self.assertEqual(workspace.plots.plot_options(**spec)["data"], str(folder))

    def test_plot_restores_campaign_recorded_before_vtu_rate(self):
        from MTS_Management.config import Config
        from MTS_Management.files import digest
        from MTS_Plotting.campaign_plot import prepared_plot

        with tempfile.TemporaryDirectory() as temporary, patch("MTS_GUI.server.ROOT", Path(temporary)):
            root = Path(temporary)
            settings = Config(totalLoad=2.0).as_dict()
            settings.pop("vtuFilesPerUnitLoad")
            settings["nrVTUFrames"] = 500
            identity = digest({"format": "mtscripts-config-v1", "settings": settings})
            campaign = root / "MTSManager/campaigns/recorded/simulations.json"
            campaign.parent.mkdir(parents=True)
            campaign.write_text(json.dumps({"name": "Recorded", "runs": [
                {"label": "Saved", "settings": settings},
                {"label": "LowerRate", "settings": settings | {"nrVTUFrames": 50}},
            ]}))
            data = root / "output"
            folder = data / f"Saved[{identity[:20]}]"
            folder.mkdir(parents=True)
            (folder / "config.conf").write_text(f"name = {folder.name}\n" + "\n".join(
                f"{key} = {value}" for key, value in settings.items()))
            (folder / "manifest.json").write_text(json.dumps({
                "settings": settings, "config_hash": identity, "executable_sha256": "fixture"}))
            (folder / "macroData.csv").write_text("load,avg_energy\n0,0.5\n1,2.5\n")

            workspace = Workspace()
            with patch.object(workspace.plots, "plot_data_roots", return_value=[data]):
                schema = workspace.plots.plot_options("Recorded", str(data))
            self.assertEqual(schema["available"], 1)
            self.assertEqual(schema["parameters"], {"vtuFilesPerUnitLoad": [100.0, 250.0]})
            spec = {"campaign": "Recorded", "data": str(data), "x": "load", "y": "avg_energy",
                    "fixed": {"vtuFilesPerUnitLoad": 250.0}}
            _, plot = prepared_plot(*workspace.plots.plot_inputs(spec))
            series, = plot["series"]
            self.assertEqual((series["x"], series["mean"]), ([0, 1], [0.5, 2.5]))

    def test_campaign_csv_refresh_uses_cached_inventory_and_downloads_only_missing_or_older_runs(self):
        from MTS_Management.cluster import Site
        from MTS_Management.config import Config
        from MTS_Management.files import file_digest, write_json

        sites = {name: Site(name=name, ssh=name) for name in ("magi", "pilot")}
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()), \
                patch("MTS_GUI.server.ROOT", Path(temporary)), \
                patch("MTS_GUI.server.STATE", Path(temporary) / ".mts"), \
                patch("MTS_Management.cluster.load_sites", return_value=sites):
            root = Path(temporary)
            workspace = Workspace()
            configs = [Config(seed=seed) for seed in range(5)]
            write_json(root / "MTSManager/campaigns/benchmark/simulations.json", {
                "name": "Benchmark", "runs": [{"label": "Run", "settings": cfg.as_dict()} for cfg in configs[:4]]})
            data = root / "downloaded"
            rows = {site: [] for site in sites}
            remote_paths = {}
            for config, name in zip(configs, ("magi", "pilot", "magi", "magi", "pilot"), strict=True):
                folder = root / name / config.folder_name("Run")
                folder.mkdir(parents=True)
                config.write(folder / "config.conf", "Run")
                manifest = folder / "manifest.json"
                write_json(manifest, {"config_hash": config.config_hash, "settings": config.as_dict()})
                progress = {}
                if config["seed"] != 3:
                    csv = folder / "macroData.csv"
                    csv.write_text("load,avg_energy\n0,1\n1,2\n")
                    os.utime(csv, (100.5, 100.5))
                    progress = {"csv_modified": 100.5, "csv_size": csv.stat().st_size}
                rows[name].append({"config_hash": config.config_hash, "folder": str(folder),
                                   "manifest_sha256": file_digest(manifest), "progress": progress})
                if config["seed"] == 4:
                    rows[name][-1]["campaign"] = "Benchmark"
                remote_paths[config.config_hash] = folder
            for name, saved in rows.items():
                workspace.monitor.inventory_cache(name, rows=saved, site=sites[name])
            for config in (configs[0], configs[2]):
                folder = remote_paths[config.config_hash]
                shutil.copytree(folder, data / folder.name)
            old = data / remote_paths[configs[0].config_hash].name / "macroData.csv"
            old.write_text("load,avg_energy\n0,1\n")
            os.utime(old, (100, 100))

            downloads = {}

            def fetch(site, manifests, destination, **options):
                downloads[site.name] = set(manifests)
                return [shutil.copytree(remote_paths[h], destination / remote_paths[h].name,
                                        dirs_exist_ok=True) for h in manifests]

            with patch("MTS_Management.cluster.fetch_many", side_effect=fetch), \
                    patch("MTS_Management.cluster.call", side_effect=AssertionError("Unexpected remote query")):
                self.assertTrue(workspace.plots.campaign_csv_outdated("Benchmark", str(data)))
                self.assertFalse(downloads)
                result = workspace.operation("plot_csv_refresh", {"campaign": "Benchmark", "data": str(data)})
                self.assertFalse(workspace.plots.campaign_csv_outdated("Benchmark", str(data)))
            self.assertEqual(downloads, {"magi": {configs[0].config_hash},
                                         "pilot": {configs[1].config_hash, configs[4].config_hash}})
            self.assertEqual(result["campaign_csv_refresh"]["downloaded"], 3)
            self.assertEqual(old.read_text(), "load,avg_energy\n0,1\n1,2\n")

    def test_campaign_refresh_discovers_attached_run_from_submission_receipt(self):
        from MTS_Management.cluster import Site
        from MTS_Management.config import Config
        from MTS_Management.files import write_json
        from MTS_Management.journal import Journal

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            planned, extra = Config(seed=1), Config(seed=2)
            manager = root / "MTSManager"
            write_json(manager / "campaigns/benchmark/simulations.json", {"name": "Benchmark", "runs": [
                {"label": "Planned", "settings": planned.as_dict()}]})
            Journal(manager / ".state").append("submission_accepted", site="fixture", campaign="Benchmark",
                                               config_hash=extra.config_hash)
            site = Site(name="fixture", ssh="fixture")
            with patch("MTS_GUI.server.ROOT", root), patch("MTS_GUI.server.STATE", root / ".mts"), \
                    patch("MTS_Management.cluster.load_sites", return_value={"fixture": site}):
                overview = Workspace().monitor.inventory_overview(campaign="Benchmark")
            self.assertEqual(set(overview["config_hashes"]), {planned.config_hash, extra.config_hash})
            self.assertEqual(overview["expected"], 1)

    def test_restart_recovers_monitor_source_and_reports_invalid_state(self):
        with tempfile.TemporaryDirectory() as temporary, patch("MTS_GUI.server.STATE", Path(temporary)):
            root = Path(temporary)
            workspace = Workspace()
            source = {"path": root / "data", "site": None, "anchor": {"folder": str(root / "data")},
                      "group": [], "manifests": {"data": "fixture"}}
            workspace.plots.run_plot_sources["@example"] = source
            workspace.plots.restart_plot_sources()
            restarted = Workspace()
            restarted.plots.restart_plot_sources(restore=True)
            self.assertEqual(restarted.plots.run_plot_source("@example"), source)
            self.assertEqual(restarted.history, [])

            (root / "restart-plot-sources.json").write_text('{"version":99,"sources":{}}')
            invalid = Workspace()
            invalid.plots.restart_plot_sources(restore=True)
            self.assertEqual(invalid.plots.run_plot_sources, {})
            self.assertEqual(invalid.task["state"], "idle")
            self.assertIn("could not be restored", invalid.history[0]["logs"][0])

    def test_animation_presets_are_reusable_without_a_campaign(self):
        with tempfile.TemporaryDirectory() as temporary, patch("MTS_GUI.server.STATE", Path(temporary)):
            workspace = Workspace()
            settings = {"fields": {"animate-kind": "energy", "animate-dpi": "300"},
                        "curve": [[0, 0], [.6, .7], [1, 1]]}
            saved = workspace.plots.presets({"name": "Energy", "selection": settings}, kind="animation")
            self.assertEqual(Workspace().plots.presets(kind="animation")["presets"][0]["selection"], settings)
            self.assertEqual(Workspace().plots.plot_presets()["presets"], [])
            updated = settings | {"curve": [[0, .4]]}
            workspace.plots.presets({"name": "Energy", "selection": updated, "id": saved["saved_id"]}, kind="animation")
            result, = Workspace().plots.presets(kind="animation")["presets"]
            self.assertEqual(result["selection"], updated)

    def test_restart_loads_new_backend_code_and_preserves_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(Path(__file__).resolve().parents[1] / "MTS_GUI", root / "MTS_GUI",
                            ignore=shutil.ignore_patterns("__pycache__"))
            with (root / "backend.log").open("w") as log:
                process = subprocess.Popen([sys.executable, "-m", "MTS_GUI", "--serve"], cwd=root,
                                           stdout=log, stderr=subprocess.STDOUT)
            def wait_for(read):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail((root / "backend.log").read_text())
                    try:
                        value = read()
                        if value:
                            return value
                    except (OSError, json.JSONDecodeError):
                        pass
                    time.sleep(0.05)
                self.fail("Backend did not become ready: " + (root / "backend.log").read_text())
            try:
                record = root / ".mts/server.json"
                saved = wait_for(lambda: json.loads(record.read_text()))
                parts = urlsplit(saved["url"])
                base = f"http://{parts.netloc}"
                headers = {"Content-Type": "application/json", "X-MTS-Token": parts.fragment}
                def health():
                    with urlopen(base + "/health", timeout=1) as response:
                        return json.load(response)
                wait_for(health)
                with patch("MTS_GUI.__main__.STATE", root / ".mts"), \
                        patch.dict("os.environ", {"MTS_DESKTOP": "0"}):
                    with self.assertRaisesRegex(RuntimeError, f"kill -TERM {process.pid}"):
                        check_server_lock()
                self.assertIsNone(process.poll())
                source = root / "MTS_GUI/server.py"
                source.write_text(source.read_text().replace('"application": "MTS",',
                                  '"application": "MTS", "generation": "reloaded",', 1))
                with urlopen(Request(base + "/api/restart", data=b"{}", headers=headers), timeout=2) as response:
                    self.assertTrue(json.load(response)["restarting"])
                wait_for(lambda: health().get("generation") == "reloaded")
                self.assertEqual(json.loads(record.read_text()), saved)
                with urlopen(Request(base + "/api/state", headers=headers), timeout=2) as response:
                    self.assertEqual(json.load(response)["task"]["state"], "idle")
                with urlopen(Request(base + "/api/quit", data=b"{}", headers=headers), timeout=2):
                    pass
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertFalse(record.exists())
                with patch("MTS_GUI.__main__.STATE", root / ".mts"):
                    check_server_lock()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    def test_inventory_cache_is_local_persistent_and_isolated_by_site_configuration(self):
        from MTS_Management.cluster import Site
        from MTS_Management.errors import ExpectedError
        sites = {name: Site(name=name, ssh=name) for name in ("first", "second")}
        rows = [{"config_hash": "example", "folder": "/work/output/example", "status": {"state": "running"}}]
        initial = {"state": "ready", "snapshot": {"rows": rows, "updated_at": 100, "revision": "first"}}
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()), \
                patch("MTS_GUI.server.STATE", Path(temporary)), \
                patch("MTS_GUI.server.ROOT", Path(temporary)), \
                patch("MTS_Management.cluster.load_sites", return_value=sites), \
                patch("MTS_Management.cluster.call", return_value=initial) as remote:
            workspace = Workspace()
            self.assertIsNone(workspace.monitor.inventory_cache("first")["updated_at"])
            remote.assert_not_called()
            workspace.operation("inventory", {"site": "first"})
            snapshot = workspace.monitor.inventory_cache("first")
            self.assertEqual(snapshot["updated_at"], 100)
            self.assertEqual(Workspace().monitor.inventory_cache("first"), snapshot)
            sites["first"].manager_directory = "~/new-manager-version"
            self.assertEqual(workspace.monitor.inventory_cache("first"), snapshot)
            self.assertIsNone(workspace.monitor.inventory_cache("second")["updated_at"])
            remote.assert_called_once()
            with patch("MTS_Management.cluster.call", side_effect=TimeoutError("unreachable")):
                with self.assertRaisesRegex(ExpectedError, "Check the SSH connection"):
                    workspace.operation("inventory", {"site": "first"})
            with patch("MTS_Management.cluster.call", return_value={"state": "started",
                       "workload": {"simulations": 2001}, "remaining_seconds": 110}):
                result = workspace.operation("inventory", {"site": "first"})
                self.assertIn("2 minutes", result["inventory_messages"][0])
                self.assertFalse(result["inventory_updated"])
            self.assertEqual(workspace.monitor.inventory_cache("first"), snapshot)
            refreshed = [rows[0] | {"status": {"state": "finished"}}]
            with patch("MTS_Management.cluster.call", side_effect=[
                    subprocess.TimeoutExpired("ssh", 60), ConnectionError("disconnected"),
                    {"state": "ready", "snapshot": {"rows": refreshed, "updated_at": 200, "revision": "second"}}]):
                workspace.operation("inventory", {"site": "first"})
            self.assertEqual(workspace.monitor.inventory_cache("first")["rows"], refreshed)
            sites["first"].output_root = "another-output"
            self.assertIsNone(workspace.monitor.inventory_cache("first")["updated_at"])
            sites["first"].output_root = "output"
            remote.return_value = {"state": "ready", "snapshot": {"rows": [], "updated_at": 300, "revision": "third"}}
            workspace.operation("inventory", {"site": "first"})
            self.assertEqual(Workspace().monitor.inventory_cache("first")["rows"], [])

    def test_local_refresh_returns_fresh_rows_without_starting_an_inventory_task(self):
        from MTS_Management.config import Config
        from MTS_Management.files import digest, write_json

        with tempfile.TemporaryDirectory() as temporary, \
                patch("MTS_GUI.server.ROOT", Path(temporary)), \
                patch("MTS_GUI.server.STATE", Path(temporary) / ".mts"):
            manager = Path(temporary) / "MTSManager"
            output = manager / "output"
            output.mkdir(parents=True)
            (manager / "sites.toml").write_text('[sites.local]\n[sites.remote]\nssh = "remote"\n')
            settings = Config(seed=7).as_dict()
            settings.pop("vtuFilesPerUnitLoad")
            settings["nrVTUFrames"] = 100
            config_hash = digest({"format": "mtscripts-config-v1", "settings": settings})
            cache = Path(temporary) / ".mts/configurations"
            cache.mkdir(parents=True)
            (cache / f"{config_hash}.conf").write_text(
                f"name = Saved[{config_hash[:20]}]\n" +
                "\n".join(f"{key} = {value}" for key, value in settings.items()))
            write_json(output / ".run-index.json", {config_hash: "missing-run"})

            workspace = Workspace()
            remote = workspace.monitor.inventory_cache("remote", rows=[])
            snapshot = workspace.monitor.refresh_local_inventory(site="local", selection={"site": "local"})

            row, = snapshot["rows"]
            self.assertEqual((row["config_hash"], row["status"]["state"], row["seed"]),
                             (config_hash, "missing", 7))
            self.assertIsNotNone(snapshot["updated_at"])
            self.assertEqual(Workspace().monitor.inventory_overview(site="local"), snapshot)
            self.assertEqual(workspace.monitor.inventory_cache("remote"), remote)
            self.assertEqual(workspace.task["state"], "idle")

    def test_campaign_reuses_cached_configurations_and_retains_failed_server_data(self):
        from MTS_Management.cluster import Site
        from MTS_Management.config import Config
        configs = [Config(seed=seed) for seed in range(3)]
        sites = {name: Site(name=name, ssh=name) for name in ("first", "second")}
        rows = [{"config_hash": config.config_hash, "folder": "example", "campaign": "Shared",
                 "attempt": None, "status": {"state": "running"}, "progress": {"fraction": 0.1}}
                for config in configs[:2]]
        rows[0]["campaign"] = "Another campaign"
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()), \
                patch("MTS_GUI.server.ROOT", Path(temporary)), \
                patch("MTS_GUI.server.STATE", Path(temporary) / ".mts"), \
                patch("MTS_Management.cluster.load_sites", return_value=sites):
            definition = Path(temporary) / "MTSManager/campaigns/shared/simulations.json"
            definition.parent.mkdir(parents=True)
            definition.write_text(json.dumps({"name": "Shared", "runs": [
                {"label": "Expected", "settings": config.as_dict()} for config in configs]}))
            workspace = Workspace()
            def campaign_snapshot(site, *args, **kwargs):
                return {"state": "ready", "snapshot": {"rows": rows[:1] if site.name == "first" else []}}

            with patch("MTS_Management.cluster.call", side_effect=campaign_snapshot) as remote:
                result = workspace.operation("inventory", {"campaign": "Shared"})
            self.assertEqual(result["updated_sites"], ["first", "second"])
            self.assertTrue(all(set(call.kwargs["config_hashes"]) == {c.config_hash for c in configs}
                                for call in remote.call_args_list))
            self.assertIsNone(workspace.monitor.inventory_cache("first")["updated_at"])
            self.assertIsNotNone(workspace.monitor.inventory_overview(campaign="Shared")["updated_at"])
            unrelated = rows[0] | {"config_hash": Config(seed=9).config_hash, "campaign": "Other"}
            first = workspace.monitor.inventory_cache("first", rows=rows[:1] + [unrelated])
            original = workspace.monitor.inventory_cache("second", rows=rows[1:])
            overview = workspace.monitor.inventory_overview(campaign="Shared")
            self.assertEqual((overview["expected"], overview["found"], overview["missing"]), (3, 2, 1))
            self.assertEqual({row["site"] for row in overview["rows"] if not row.get("missing")}, set(sites))
            def unavailable_second(site, *args, **kwargs):
                if site.name == "second":
                    raise TimeoutError("offline")
                return campaign_snapshot(site)

            with patch("MTS_Management.cluster.call", side_effect=unavailable_second):
                result = workspace.operation("inventory", {"campaign": "Shared"})
            self.assertEqual(result["updated_sites"], ["first"])
            self.assertIn("second", result["errors"])
            self.assertIn(unrelated, workspace.monitor.inventory_cache("first")["rows"])
            self.assertEqual(workspace.monitor.inventory_cache("first")["updated_at"], first["updated_at"])
            self.assertEqual(workspace.monitor.inventory_cache("second"), original)
            with patch("MTS_GUI.server.threading.Thread") as worker:
                workspace.start("Inventory", lambda: workspace.operation("inventory", {"campaign": "Shared"}),
                                cancellable=True)
                workspace.abort()
                worker.call_args.kwargs["target"]()
            self.assertEqual(workspace.task["state"], "aborted")
            self.assertEqual(workspace.monitor.inventory_cache("second"), original)
            workspace.monitor.inventory_cache("first", rows=[], config_hashes=[c.config_hash for c in configs])
            self.assertEqual(Workspace().monitor.inventory_cache("first")["rows"], [unrelated])

    def test_monitoring_keeps_recorded_campaign_identity_when_runtime_schema_changes(self):
        from MTS_Management.campaign import campaign_entries
        from MTS_Management.config import Config
        from MTS_Management.files import digest
        with tempfile.TemporaryDirectory() as temporary, patch("MTS_GUI.server.ROOT", Path(temporary)):
            path = Path(temporary) / "MTSManager/campaigns/recorded/simulations.json"
            path.parent.mkdir(parents=True)
            settings = Config().as_dict()
            settings.pop("tangentBackend")
            identity = digest({"format": "mtscripts-config-v1", "settings": settings})
            path.write_text(json.dumps({"name": "Recorded", "runs": [{"label": "Saved", "settings": settings}]}))
            self.assertEqual(Workspace().monitor.campaign_definitions(), {"Recorded": {identity: f"Saved[{identity[:20]}]"}})
            self.assertEqual(Workspace().status()["campaigns"], ["Recorded"])
            with self.assertRaisesRegex(ValueError, "complete"):
                campaign_entries(path)

    def test_monitor_groups_seeds_by_settings_instead_of_labels(self):
        from MTS_Management.cluster import Site
        from MTS_Management.config import Config
        configs = [Config(seed=8), Config(seed=2), Config(seed=2, totalLoad=3)]
        with tempfile.TemporaryDirectory() as temporary, \
                patch("MTS_GUI.server.ROOT", Path(temporary)), \
                patch("MTS_GUI.server.STATE", Path(temporary) / ".mts"), \
                patch("MTS_Management.cluster.load_sites", return_value={"cluster": Site(name="cluster", ssh="cluster")}):
            path = Path(temporary) / "MTSManager/campaigns/example/simulations.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"name": "Example", "runs": [
                {"label": "SameLabel", "settings": c.as_dict()} for c in configs]}))
            workspace = Workspace()
            workspace.monitor.inventory_cache("cluster", rows=[{
                "config_hash": configs[0].config_hash, "folder": "DifferentLabel", "campaign": "Example"}])
            rows = {row["config_hash"]: row for row in workspace.monitor.inventory_overview(campaign="Example")["rows"]}
            first, second, different = [rows[c.config_hash] for c in configs]
            self.assertEqual(first["settings_group"], second["settings_group"])
            self.assertNotEqual(first["settings_group"], different["settings_group"])
            self.assertEqual([rows[c.config_hash]["seed"] for c in configs], [8, 2, 2])
            self.assertEqual(first["folder"], "DifferentLabel")

    def test_monitor_plot_and_archive_use_exact_run_identities(self):
        from MTS_Management.cluster import Site
        from MTS_Management.config import Config
        from MTS_Management.errors import ExpectedError
        from MTS_Management.files import write_json
        from MTS_Plotting.campaign_plot import prepared_plot

        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            root = Path(temporary).resolve()
            site = Site(name="local", directory=str(root))
            configs = [Config(seed=seed) for seed in (1, 2)]
            output = root / "output"
            definition = root / "MTSManager/campaigns/gui/example/simulations.json"
            write_json(definition, {"name": "Example", "runs": [
                {"label": f"Seed{c['seed']}", "settings": c.as_dict()} for c in configs]})
            folders = []
            for config in configs:
                folder = output / config.folder_name(f"Seed{config['seed']}")
                attempt = folder / "attempts/attempt"
                attempt.mkdir(parents=True)
                config.write(folder / "config.conf", f"Seed{config['seed']}")
                write_json(folder / "manifest.json", {"settings": config.as_dict(), "campaign": "Example",
                           "config_hash": config.config_hash, "executable_sha256": "fixture"})
                write_json(attempt / "request.json", {"campaign": "Example", "created_at": "2026-01-01"})
                write_json(attempt / "status.json", {"state": "finished"})
                (folder / "macroData.csv").write_text(f"load,avg_sigma12\n0,0\n1,{config['seed']}\n2,")
                folders.append(folder)
            sites = {"local": site}
            with patch("MTS_GUI.server.ROOT", root), patch("MTS_GUI.server.STATE", root / ".mts"), \
                    patch("MTS_Management.cluster.load_sites", return_value=sites):
                workspace = Workspace()
                scope = {"campaign": "Example"}
                workspace.operation("inventory", scope)
                rows = {row["config_hash"]: row for row in workspace.monitor.inventory_overview(**scope)["rows"]}
                selections = [{"scope": scope, "runs": [rows[c.config_hash]]} for c in configs]
                for extra, executable in [(Config(seed=3, nrThreads=2), "fixture"), (Config(seed=4), "different")]:
                    extra_folder = output / extra.folder_name()
                    write_json(extra_folder / "manifest.json", {"settings": extra.as_dict(),
                               "config_hash": extra.config_hash, "executable_sha256": executable})
                result = workspace.plots.open_run_plot(selections[1])
                self.assertTrue(result["has_other_seeds"])
                self.assertFalse(result["include_seeds"])
                spec = {"campaign": result["plot_selection"], "x": "load", "y": "avg_sigma12", "seed": "all"}
                schema, plot = prepared_plot(*workspace.plots.plot_inputs(spec))
                self.assertEqual(schema["data"], str(folders[1]))
                options = workspace.plots.animation_options(spec)
                self.assertEqual((options["boundary"], options["preview_frame"]), ("fixed", "middle"))
                cached = workspace.monitor.inventory_cache("local", site=site)["rows"]
                workspace.monitor.inventory_cache("local", site=site, rows=[
                    row | {"status": {"state": "running"}} if row["config_hash"] == configs[1].config_hash else row
                    for row in cached])
                self.assertEqual(workspace.plots.animation_options(spec)["preview_frame"], "last")
                workspace.monitor.inventory_cache("local", site=site, rows=cached)
                self.assertEqual(plot["series"][0]["mean"], [0, 2])
                self.assertEqual(plot["runs"], 1)
                expanded = workspace.plots.open_run_plot({"selection": result["plot_selection"], "include_seeds": True})
                self.assertEqual(workspace.plots.animation_options({"campaign": expanded["plot_selection"]}), {})
                self.assertEqual(workspace.plots.animation_source({"campaign": "Example", "data": str(output), "seed": 2}),
                                 folders[1])
                _, plot = prepared_plot(*workspace.plots.plot_inputs(spec | {"campaign": expanded["plot_selection"]}))
                self.assertEqual(plot["series"][0]["mean"], [0, 1.5])
                self.assertEqual(plot["series"][0]["seed_ids"], [1, 2])
                group = workspace.plots.open_run_plot({"scope": scope, "runs": list(rows.values())})
                self.assertTrue(group["include_seeds"])

                status = folders[1] / "attempts/attempt/status.json"
                write_json(status, {"state": "running"})
                with self.assertRaisesRegex(ExpectedError, "all attempts have stopped"):
                    workspace.monitor.archive_monitor({"scope": scope, "runs": list(rows.values())})
                self.assertTrue(all(folder.is_dir() for folder in folders))
                write_json(status, {"state": "finished"})

                workspace.monitor.archive_monitor(selections[0])
                archive = root / "output-archive"
                self.assertTrue((archive / folders[0].name / "macroData.csv").is_file())
                self.assertFalse(folders[0].exists())
                overview = workspace.monitor.inventory_overview(**scope)
                self.assertEqual((overview["found"], overview["missing"], overview["archived"]), (1, 0, 1))
                self.assertTrue(definition.is_file())
                sites["offline"] = Site(name="offline", ssh="offline")
                with patch("MTS_Management.cluster.across", side_effect=AssertionError("Unexpected SSH check")):
                    result = workspace.monitor.archive_monitor(selections[1])
                self.assertFalse(result["errors"])
                self.assertIn("Verification is pending for offline", result["campaign_notes"][0])
                self.assertTrue(definition.is_file())
                self.assertFalse(folders[1].exists())
                del sites["offline"]
                workspace.operation("archive_campaign", scope)
                self.assertTrue((root / "MTSManager/campaigns-archive/example/simulations.json").is_file())
                self.assertEqual(workspace.monitor.inventory_overview(site="local")["rows"], [])

                workspace = Workspace()
                entries = workspace.monitor.archive_history()
                campaign = next(row for row in entries if row["kind"] == "campaign")
                run = next(row for row in entries if row["output_folder"] == str(folders[1]))
                workspace.monitor.restore_archive(campaign["event_id"])
                self.assertTrue(definition.is_file())
                folders[1].mkdir()
                with self.assertRaisesRegex(ExpectedError, "Destination already exists"):
                    workspace.monitor.restore_archive(run["event_id"])
                self.assertTrue((archive / folders[1].name / "macroData.csv").is_file())
                folders[1].rmdir()
                workspace.monitor.restore_archive(run["event_id"])
                self.assertEqual((folders[1] / "macroData.csv").read_text(), "load,avg_sigma12\n0,0\n1,2\n2,")
                restored, = workspace.monitor.inventory_overview(site="local")["rows"]
                self.assertEqual(restored["config_hash"], configs[1].config_hash)
                self.assertEqual(sum(row["status"] == "Restored" for row in workspace.monitor.archive_history()), 2)

    def test_clearing_terminal_does_not_release_a_running_operation(self):
        workspace = Workspace()
        with patch("MTS_GUI.server.threading.Thread") as worker, redirect_stdout(StringIO()):
            workspace.history.append({"id": "previous", "state": "complete"})
            workspace.start("Inventory", lambda: {"site": "first"})
            workspace.clear_terminal()
            self.assertEqual(workspace.history, [workspace.task])
            with self.assertRaises(ValueError):
                workspace.start("Run", lambda: None)
            worker.call_args.kwargs["target"]()
        self.assertEqual(workspace.task["state"], "complete")
        self.assertEqual(workspace.task["result"], {"site": "first"})
        workspace.clear_terminal()
        self.assertEqual(workspace.history, [])

    def test_local_operations_require_session_token_and_same_origin(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = Workspace()
        server.authority = f"127.0.0.1:{server.server_port}"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://{server.authority}/api/quit"
            for headers in ({}, {"X-MTS-Token": server.app.token, "Origin": "https://example.com"}):
                request = Request(url, data=b"{}", headers={"Content-Type": "application/json", **headers})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=2)
                self.assertEqual(error.exception.code, 403)
            with urlopen(Request(f"http://{server.authority}/api/state",
                         headers={"X-MTS-Token": server.app.token}), timeout=2) as response:
                self.assertEqual(json.load(response)["task"]["state"], "idle")
            server.app.task["state"] = "running"
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(f"http://{server.authority}/api/restart", data=b"{}",
                        headers={"Content-Type": "application/json", "X-MTS-Token": server.app.token}), timeout=2)
            self.assertEqual(error.exception.code, 400)
            self.assertFalse(server.app.stopping)
            server.app.task["state"] = "idle"
            traversal = f"http://{server.authority}/figures/%2e%2e/%2e%2e/pyproject.toml"
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(traversal, headers={"X-MTS-Token": server.app.token}), timeout=2)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_setup_never_checks_out_over_populated_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, marker in (("MTS2D", "CMakeLists.txt"), ("MTSManager", "pyproject.toml"),
                                 ("MTSPlot", "pyproject.toml")):
                (root / name).mkdir()
                (root / name / marker).write_text("local scientific changes")
            with patch("MTS_GUI.install.ROOT", root), patch("MTS_GUI.install.run") as execute:
                synchronize_components(lambda _: None)
                execute.assert_not_called()
                (root / "MTSPlot/pyproject.toml").unlink()
                (root / ".git").mkdir()
                synchronize_components(lambda _: None)
                self.assertEqual(execute.call_args.args[0],
                                 ["git", "submodule", "update", "--init", "--recursive", "--", "MTSPlot"])


if __name__ == "__main__":
    unittest.main()
