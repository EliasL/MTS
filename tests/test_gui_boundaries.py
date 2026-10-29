"""Protect privileged local operations and existing scientific source checkouts."""

import json
from contextlib import redirect_stdout
from io import StringIO
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

from MTS_GUI.install import synchronize_components
from MTS_GUI.__main__ import check_server_lock
from MTS_GUI.server import Handler, ThreadingHTTPServer, Workspace


class Boundaries(unittest.TestCase):
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
        sites = {name: Site(name=name, ssh=name) for name in ("first", "second")}
        rows = [{"config_hash": "example", "status": {"state": "running"}}]
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()), \
                patch("MTS_GUI.server.STATE", Path(temporary)), \
                patch("MTS_GUI.server.ROOT", Path(temporary)), \
                patch("MTS_Management.cluster.load_sites", return_value=sites), \
                patch("MTS_Management.cluster.call", return_value=rows) as remote:
            workspace = Workspace()
            self.assertIsNone(workspace.inventory_cache("first")["updated_at"])
            remote.assert_not_called()
            workspace.operation("inventory", {"site": "first"})
            snapshot = workspace.inventory_cache("first")
            self.assertEqual(Workspace().inventory_cache("first"), snapshot)
            sites["first"].manager_directory = "~/new-manager-version"
            self.assertEqual(workspace.inventory_cache("first"), snapshot)
            self.assertIsNone(workspace.inventory_cache("second")["updated_at"])
            remote.assert_called_once()
            with patch("MTS_Management.cluster.call", side_effect=TimeoutError("unreachable")):
                with self.assertRaises(TimeoutError):
                    workspace.operation("inventory", {"site": "first"})
            self.assertEqual(workspace.inventory_cache("first"), snapshot)
            sites["first"].output_root = "another-output"
            self.assertIsNone(workspace.inventory_cache("first")["updated_at"])
            sites["first"].output_root = "output"
            remote.return_value = []
            workspace.operation("inventory", {"site": "first"})
            self.assertEqual(Workspace().inventory_cache("first")["rows"], [])

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
            with patch("MTS_Management.cluster.call", side_effect=[rows[:1], []]) as remote:
                result = workspace.operation("inventory", {"campaign": "Shared"})
            self.assertEqual(result["updated_sites"], ["first", "second"])
            self.assertTrue(all(set(call.kwargs["config_hashes"]) == {c.config_hash for c in configs}
                                for call in remote.call_args_list))
            self.assertIsNone(workspace.inventory_cache("first")["updated_at"])
            self.assertIsNotNone(workspace.inventory_overview(campaign="Shared")["updated_at"])
            unrelated = rows[0] | {"config_hash": Config(seed=9).config_hash, "campaign": "Other"}
            first = workspace.inventory_cache("first", rows=rows[:1] + [unrelated])
            original = workspace.inventory_cache("second", rows=rows[1:])
            overview = workspace.inventory_overview(campaign="Shared")
            self.assertEqual((overview["expected"], overview["found"], overview["missing"]), (3, 2, 1))
            self.assertEqual({row["site"] for row in overview["rows"] if not row.get("missing")}, set(sites))
            with patch("MTS_Management.cluster.call", side_effect=[rows[:1], TimeoutError("offline")]):
                result = workspace.operation("inventory", {"campaign": "Shared"})
            self.assertEqual(result["updated_sites"], ["first"])
            self.assertIn("second", result["errors"])
            self.assertIn(unrelated, workspace.inventory_cache("first")["rows"])
            self.assertEqual(workspace.inventory_cache("first")["updated_at"], first["updated_at"])
            self.assertEqual(workspace.inventory_cache("second"), original)
            with patch("MTS_GUI.server.threading.Thread") as worker:
                workspace.start("Inventory", lambda: workspace.operation("inventory", {"campaign": "Shared"}),
                                cancellable=True)
                workspace.abort()
                worker.call_args.kwargs["target"]()
            self.assertEqual(workspace.task["state"], "aborted")
            self.assertEqual(workspace.inventory_cache("second"), original)
            workspace.inventory_cache("first", rows=[], config_hashes=[c.config_hash for c in configs])
            self.assertEqual(Workspace().inventory_cache("first")["rows"], [unrelated])

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
            self.assertEqual(Workspace().campaign_definitions(), {"Recorded": {identity: f"Saved[{identity[:20]}]"}})
            self.assertEqual(Workspace().status()["campaigns"], ["Recorded"])
            with self.assertRaisesRegex(ValueError, "complete"):
                campaign_entries(path)

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
