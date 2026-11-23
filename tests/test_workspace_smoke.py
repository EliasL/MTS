"""Exercise CLI submission and plotting with the same native run output."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from MTS_Management.cluster import load_sites
from MTS_Management.config import Config, DEFAULTS
from MTS_Management.files import digest, file_digest, write_json
from MTS_Management.provenance import verify_schema
from MTS_Management.submission import submit_campaign
from MTS_Plotting.campaign_plot import prepared_plot


ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("MTS_TEST_BINARY")


@unittest.skipUnless(BINARY, "Set MTS_TEST_BINARY to run the workspace smoke test.")
class WorkspaceSmoke(unittest.TestCase):
    def test_submit_inventory_and_plot(self):
        verify_schema(ROOT / "MTS2D")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executable = root / "MTS2D"
            shutil.copy2(BINARY, executable)
            write_json(executable.with_name("build-manifest.json"), {
                "branch": "main", "revision": "workspace-smoke",
                "execution_interface": "folder-v1", "executable_sha256": file_digest(executable),
                "configuration_keys_sha256": digest(sorted(DEFAULTS)),
            })
            sites = root / "sites.toml"
            sites.write_text('[sites.local]\ndirectory = "."\nexecutable = "MTS2D"\nmode = "foreground"\n')
            config = Config(rows=4, cols=4, boundary="periodic", initialGuessNoise=0.0,
                            loading="noRelaxation", totalLoad=0.002, loadIncrement=0.001,
                            tangentBackend="eigen", writeMeshVTUs=False, writeDumps=False)
            native = root / "input.conf"
            config.write(native)
            command = [sys.executable, "-m", "MTS_Management", "--sites", str(sites)]
            receipt = json.loads(subprocess.check_output(command + ["run", str(native)], text=True))
            job, = receipt["jobs"]
            folder = Path(job["output_folder"])
            self.assertEqual(receipt["config_hash"], config.config_hash)

            campaign = root / "plan/simulations.json"
            second = Config(**(config.as_dict() | {"seed": 1}))
            write_json(campaign, {"name": "Smoke", "runs": [
                {"label": "First", "settings": config.as_dict()},
                {"label": "Second", "settings": second.as_dict()},
            ]})
            plan = json.loads(subprocess.check_output(command + ["validate-campaign", str(campaign)], text=True))
            self.assertEqual({member["config_hash"] for member in plan["members"]},
                             {config.config_hash, second.config_hash})
            run = json.loads(subprocess.check_output(command + ["inspect-run", str(folder)], text=True))
            self.assertEqual(run["manifest"]["settings"], config.as_dict())
            collection = folder.parent
            _, plot = prepared_plot(campaign, collection, {"x": "load", "y": "avg_energy", "seed": config["seed"]})
            series, = plot["series"]
            self.assertEqual(series["x"], [0.0, 0.001, 0.002])
            self.assertGreater(series["mean"][-1], series["mean"][0])

            site = load_sites(sites)["local"]
            with patch("MTS_Management.campaign.__file__", str(root / "manager/MTS_Management/campaign.py")):
                batch = submit_campaign(site, campaign, state_dir=root / ".state")
            second_job, = batch["jobs"]
            self.assertEqual(second_job["config_hash"], second.config_hash)
            self.assertEqual(batch["skipped"], [{"label": "First", "config_hash": config.config_hash}])
            self.assertEqual(len(list((folder / "attempts").iterdir())), 1)
            rows = json.loads(subprocess.check_output(command + ["inventory"], text=True))
            self.assertEqual({row["config_hash"]: row["status"]["state"] for row in rows},
                             {config.config_hash: "finished", second.config_hash: "finished"})
