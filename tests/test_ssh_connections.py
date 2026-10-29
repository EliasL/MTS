"""Regression checks for SSH trust, credentials and disabled destinations."""

import base64
import os
import tomllib
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from MTS_Management.cluster import call, load_sites
from MTS_GUI import ssh_connections as ssh
from MTS_GUI.server import Workspace


class SSHConnections(unittest.TestCase):
    def test_disabling_destination_excludes_all_profiles_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            site_file = Path(temporary) / "sites.toml"
            text = '[sites.first]\nssh="cluster"\n[sites.second]\nssh="cluster"\n[sites.local]\n'
            site_file.write_text(text)
            ssh.set_disabled(site_file, "cluster", True)
            self.assertEqual(list(load_sites(site_file)), ["local"])
            disabled = load_sites(site_file, include_disabled=True)["first"]
            with self.assertRaisesRegex(ValueError, "disabled"), patch("subprocess.Popen") as remote:
                call(disabled, "info")
            remote.assert_not_called()
            self.assertEqual(site_file.read_text(), text)
            self.assertTrue(ssh.connections(site_file)[0]["disabled"])
            ssh.set_disabled(site_file, "cluster", False)
            self.assertEqual(set(load_sites(site_file)), {"first", "second", "local"})
            ssh.register_connections(site_file, ["account-only"])
            ssh.set_disabled(site_file, "account-only", True)
            self.assertTrue(next(row for row in ssh.connections(site_file)
                                 if row["host"] == "account-only")["disabled"])
            ssh.set_disabled(site_file, "account-only", False)
            account = next(row for row in ssh.connections(site_file) if row["host"] == "account-only")
            self.assertEqual(account["profiles"], [])
            self.assertFalse(account["disabled"])
            self.assertEqual(set(load_sites(site_file)), {"first", "second", "local"})

    def test_acceptance_refuses_changed_key_without_modifying_known_hosts(self):
        with tempfile.TemporaryDirectory() as temporary:
            known = Path(temporary) / "known_hosts"
            known.write_text("existing key\n")
            proposal = {"file": str(known), "fingerprint": "reviewed"}
            with patch.object(ssh, "scan_key", return_value=proposal | {"fingerprint": "changed"}), \
                    patch.object(ssh.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "changed"):
                    ssh.accept_key("cluster", proposal)
                run.assert_not_called()
            self.assertEqual(known.read_text(), "existing key\n")

    def test_acceptance_replaces_only_reviewed_destination_and_retains_backup(self):
        encoded = base64.b64encode(bytes.fromhex("0000000b") + b"ssh-ed25519" +
                                   bytes.fromhex("00000020") + bytes(32)).decode()
        with tempfile.TemporaryDirectory() as temporary:
            known = Path(temporary) / "known_hosts"
            original = f"[cluster]:2222 ssh-ed25519 {encoded}\nother ssh-ed25519 {encoded}\n"
            known.write_text(original)
            proposal = {"host": "cluster", "file": str(known), "lookup": "[cluster]:2222",
                        "fingerprint": "reviewed", "key": f"[cluster]:2222 ssh-ed25519 {encoded} new\n"}
            with patch.object(ssh, "scan_key", return_value=proposal):
                ssh.accept_key("cluster", proposal)
            self.assertEqual(known.with_suffix(".old").read_text(), original)
            self.assertIn(f"other ssh-ed25519 {encoded}\n", known.read_text())
            self.assertEqual(known.read_text().count("[cluster]:2222"), 1)
            self.assertIn(proposal["key"], known.read_text())

    def test_added_alias_preserves_global_ssh_defaults_and_existing_hosts(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".ssh/config"
            config.parent.mkdir()
            config.write_text("ServerAliveInterval 47\nHost existing\n    HostName old.example.org\n")
            sites = home / "sites.toml"
            sites.write_text("[sites.local]\n")
            with patch.object(ssh.Path, "home", return_value=home):
                ssh.add_connection(sites, {"name": "cluster", "hostname": "new.example.org",
                    "user": "scientist", "port": 2222, "directory": "~/work", "source": "../source",
                    "output_root": "output"})
            def settings(host):
                output = subprocess.check_output(["ssh", "-G", "-F", str(config), host],
                                                 text=True, stderr=subprocess.DEVNULL)
                return dict(line.split(" ", 1) for line in output.splitlines())
            self.assertEqual(settings("existing")["serveraliveinterval"], "47")
            self.assertEqual(settings("existing")["hostname"], "old.example.org")
            self.assertEqual(settings("cluster")["hostname"], "new.example.org")
            self.assertEqual(settings("cluster")["port"], "2222")
            self.assertEqual(load_sites(sites)["cluster"].directory, "~/work")

    def test_changed_host_key_is_proposed_without_automatic_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            site_file = Path(temporary) / "sites.toml"
            site_file.write_text('[sites.cluster]\nssh="cluster"\n')
            failure = subprocess.CompletedProcess([], 255, "", "REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.")
            proposals = {}
            with patch.object(ssh.subprocess, "run", return_value=failure), \
                    patch.object(ssh, "scan_key", return_value={"fingerprint": "SHA256:review"}), \
                    patch.object(ssh, "accept_key") as accept:
                result = ssh.check_connections(site_file, lambda _: None, proposals)
            self.assertTrue(result["connections"][0]["repair"])
            self.assertEqual(proposals["cluster"]["fingerprint"], "SHA256:review")
            accept.assert_not_called()

    def test_host_key_probe_uses_isolated_file_and_keeps_retry_available_on_failure(self):
        encoded = base64.b64encode(bytes.fromhex("0000000b") + b"ssh-ed25519" +
                                   bytes.fromhex("00000020") + bytes(32)).decode()
        with tempfile.TemporaryDirectory() as temporary:
            known = Path(temporary) / "known_hosts"
            known.write_text("existing key\n")
            settings = {"hostname": "cluster.example", "port": "22", "proxyjump": "gateway",
                        "userknownhostsfile": str(known)}
            captured_files = []
            def capture(command, **kwargs):
                path = Path(next(option.split("=", 1)[1] for option in command
                                 if option.startswith("UserKnownHostsFile=")))
                captured_files.append(path)
                path.write_text(f"cluster.example ssh-ed25519 {encoded}\n")
                return subprocess.CompletedProcess(command, 255, "", "Permission denied")
            with patch.object(ssh, "effective", return_value=settings), \
                    patch.object(ssh.subprocess, "run", side_effect=capture):
                proposal = ssh.scan_key("cluster")
            self.assertTrue(proposal["fingerprint"].startswith("SHA256:"))
            self.assertEqual(known.read_text(), "existing key\n")
            self.assertFalse(captured_files[0].exists())
            sites = Path(temporary) / "sites.toml"
            sites.write_text('[sites.cluster]\nssh="cluster"\n')
            failure = subprocess.CompletedProcess([], 255, "", "Host key verification failed.")
            with patch.object(ssh.subprocess, "run", return_value=failure), \
                    patch.object(ssh, "scan_key", side_effect=ValueError("Temporary connection failure")):
                row = ssh.check_connections(sites, lambda _: None, {})["connections"][0]
            self.assertTrue(row["repair"])
            self.assertIsNone(row["fingerprint"])
            self.assertIn("Retry", row["suggestion"])

    def test_profile_editor_preserves_other_profiles_and_nested_scientific_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            sites = Path(temporary) / "sites.toml"
            source = Path(__file__).resolve().parents[1] / "MTSManager/sites.toml"
            sites.write_text(source.read_text())
            original = tomllib.loads(sites.read_text())["sites"]
            values = original["magi"] | {"directory": "~/simulation/MTS"}
            ssh.save_profile(sites, "magi", {"name": "magi", "original_name": "magi", "settings":
                {key: value for key, value in values.items() if key != "ssh"}})
            saved = tomllib.loads(sites.read_text())["sites"]
            self.assertEqual({key: value for key, value in saved.items() if key != "magi"},
                             {key: value for key, value in original.items() if key != "magi"})
            self.assertEqual(saved["magi"]["slurm"], original["magi"]["slurm"])
            self.assertEqual(saved["magi"]["environment"], original["magi"]["environment"])
            self.assertEqual(load_sites(sites)["magi"].directory, "~/simulation/MTS")
            before = sites.read_text()
            with self.assertRaises(ValueError):
                ssh.save_profile(sites, "other-host", {"name": "magi", "original_name": "magi", "settings": {}})
            self.assertEqual(sites.read_text(), before)

    def test_ssh_editor_preserves_other_hosts_and_clears_proxy_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".ssh/config"
            config.parent.mkdir()
            config.write_text('ServerAliveInterval 47\nHost cluster\n HostName old.example\n User olduser\n'
                              ' ProxyJump gateway\n IdentityFile "~/.ssh/key with spaces"\n'
                              ' ForwardAgent yes\nHost other\n HostName other.example\n')
            sites = home / "sites.toml"
            sites.write_text('[sites.fixture]\nssh="cluster"\n')
            def settings(host):
                result = subprocess.run(["ssh", "-G", "-F", str(config), host],
                                        capture_output=True, text=True, check=True)
                values = {}
                for line in result.stdout.splitlines():
                    key, value = line.split(" ", 1)
                    if key == "identityfile":
                        values.setdefault(key, []).append(value)
                    else:
                        values[key] = value
                return values
            with patch.object(ssh.Path, "home", return_value=home), \
                    patch.object(ssh, "effective", side_effect=settings):
                data = ssh.editor_settings(sites, "cluster")["connection"]
                ssh.configure_address("cluster", data | {"hostname": "new.example", "user": "scientist",
                                                        "proxyjump": "none", "extra": "ForwardAgent no"})
                saved = settings("cluster")
                self.assertEqual(saved["hostname"], "new.example")
                self.assertEqual(saved["user"], "scientist")
                self.assertEqual(saved.get("proxyjump", "none"), "none")
                self.assertIn("~/.ssh/key with spaces", saved["identityfile"])
                self.assertEqual(saved["forwardagent"], "no")
                self.assertEqual(settings("other")["hostname"], "other.example")
                self.assertEqual(settings("other")["serveraliveinterval"], "47")
                original = config.read_text()
                with self.assertRaises(ValueError):
                    ssh.configure_address("cluster", data | {"extra": "Host=other"})
                self.assertEqual(config.read_text(), original)

    def test_inspection_suggests_profile_without_creating_remote_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            binary = home / "bin"
            binary.mkdir()
            for name in ("python3", "sbatch"):
                command = binary / name
                command.write_text("#!/bin/sh\nprintf '3.12.1\\n'\n")
                command.chmod(0o700)
            source = home / "simulation/MTS2D-current"
            source.mkdir(parents=True)
            (source / "CMakeLists.txt").write_text("fixture")
            before = set(home.rglob("*"))
            execute = subprocess.run
            def local_inspection(command, **kwargs):
                return execute(["/bin/bash", "-s"], env=os.environ | {"HOME": str(home),
                    "PATH": str(binary) + ":/usr/bin:/bin"}, **kwargs)
            with patch.object(ssh.subprocess, "run", side_effect=local_inspection):
                result = ssh.inspect_server("fixture", lambda _: None)
            self.assertEqual(result["suggestions"]["mode"], "slurm")
            self.assertEqual(result["suggestions"]["source"], "MTS2D")
            self.assertEqual(result["suggestions"]["python"], str(binary / "python3"))
            self.assertEqual(set(home.rglob("*")), before)

    def test_deployment_bootstraps_missing_profile_workspace(self):
        from MTS_Management.deployments import update_managers
        from MTS_Management.provenance import scripts_identity
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "new workspace"
            sites = root / "sites.toml"
            sites.write_text(f'[sites.fixture]\nssh="fixture"\ndirectory={__import__("json").dumps(str(workspace))}\n')
            run = subprocess.run
            def remote(command, **kwargs):
                if command[0] == "ssh":
                    return run(["bash", "-c", command[-1]], **kwargs)
                return subprocess.CompletedProcess(command, 0, "", "")
            def verify(site, *args, **kwargs):
                self.assertTrue(workspace.is_dir())
                return {"scripts": scripts_identity()}
            with patch("MTS_Management.deployments.subprocess.run", side_effect=remote), \
                    patch("MTS_Management.deployments.deploy"), patch("MTS_Management.deployments.call", side_effect=verify):
                # Keep the shared installation fixture inside the temporary HOME.
                with patch.dict(os.environ, {"HOME": str(root)}):
                    result = update_managers(sites, host="fixture", log=lambda _: None)
            self.assertEqual(result["updated_sites"], ["fixture"])

    def test_install_password_is_excluded_from_terminal_and_task_result(self):
        password = "sensitive-example-password"
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()), \
                patch("MTS_GUI.server.ROOT", Path(temporary)), \
                patch.object(ssh, "connections", return_value=[{"host": "cluster", "disabled": False}]), \
                patch.object(ssh, "install_key", return_value={"installed": "cluster"}) as install:
            workspace = Workspace()
            result = workspace.operation("ssh_install", {"host": "cluster", "password": password})
            install.assert_called_once_with("cluster", password, workspace.log)
            self.assertNotIn(password, str(workspace.task) + str(result))

    def test_shared_manager_transport_quotes_paths_and_respects_failed_cd(self):
        with tempfile.TemporaryDirectory(prefix="mts space '") as temporary:
            root = Path(temporary)
            manager = root / "manager's folder"
            package = manager / "fixture"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "__main__.py").write_text('import json,sys; print(json.dumps({"received": json.load(sys.stdin)["operation"]}))')
            site_file = root / "sites.toml"
            site_file.write_text('[sites.remote]\nssh="cluster"\n')
            site = load_sites(site_file)["remote"]
            stale = root / "MTS_Management"
            stale.mkdir()
            (stale / "__init__.py").write_text("")
            (stale / "__main__.py").write_text('raise RuntimeError("stale worker package")')
            site.python = sys.executable
            site.directory = str(root)
            site.manager_directory = str(manager)
            # Use the actual installed module via a symlink; the transport selects MTS_Management.
            (manager / "MTS_Management").symlink_to(package, target_is_directory=True)
            original = subprocess.Popen
            def local_shell(args, **kwargs):
                return original(["bash", "-c", args[-1]], **kwargs)
            with patch("MTS_Management.cluster.subprocess.Popen", side_effect=local_shell):
                self.assertEqual(call(site, "info"), {"received": "info"})
                site.directory = str(root / "absent")
                with self.assertRaisesRegex(RuntimeError, "No such file or directory"):
                    call(site, "info")


if __name__ == "__main__":
    unittest.main()
