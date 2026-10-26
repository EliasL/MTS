"""Protect privileged local operations and existing scientific source checkouts."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from MTS_GUI.install import synchronize_components
from MTS_GUI.server import Handler, ThreadingHTTPServer, Workspace


class Boundaries(unittest.TestCase):
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
