"""Local OpenSSH connection administration with explicit host-key approval."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from MTS_Management.cluster import Site, load_sites, shell_path
from MTS_Management.deployments import SSH, deployment_expectation, check_deployment
from MTS_Management.files import locked, read_json, write_json


def connections(site_file):
    result = {}
    for site in load_sites(site_file, include_disabled=True).values():
        if site.ssh:
            row = result.setdefault(site.ssh, {"host": site.ssh, "profiles": [], "disabled": True})
            row["profiles"].append(site.name)
            row["disabled"] &= site.disabled
    preferences = Path(site_file).parent / ".state/ssh-connections.json"
    if preferences.exists():
        for host, settings in read_json(preferences).items():
            if settings.get("listed"):
                result.setdefault(host, {"host": host, "profiles": [],
                                         "disabled": settings.get("disabled", False)})
    return list(result.values())


def register_connections(site_file, aliases):
    """List configured SSH accounts without inventing simulation site paths."""
    for alias in aliases:
        Site(name="SSH", ssh=alias)
        if not alias:
            raise ValueError("An SSH alias is required")
    path = Path(site_file).parent / ".state/ssh-connections.json"
    with locked(path.with_suffix(".lock")):
        data = read_json(path) if path.exists() else {}
        for alias in aliases:
            data.setdefault(alias, {})["listed"] = True
        write_json(path, data)


def set_disabled(site_file, host, disabled):
    if type(disabled) is not bool:
        raise ValueError("disabled must be a boolean")
    if host not in {r["host"] for r in connections(site_file)}:
        raise ValueError("Unknown connection")
    path = Path(site_file).parent / ".state/ssh-connections.json"
    with locked(path.with_suffix(".lock")):
        data = read_json(path) if path.exists() else {}
        data.setdefault(host, {})["disabled"] = disabled
        write_json(path, data)
    return {"host": host, "disabled": disabled}


def effective(host):
    result = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, check=True, timeout=10)
    settings = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "identityfile":
            settings.setdefault(key, []).append(value)
        else:
            settings[key] = value
    return settings


def scan_key(host):
    settings = effective(host)
    hostname, port = settings["hostname"], int(settings["port"])
    # Use the configured SSH route, including proxies, to record the offered key
    # in an isolated file. Disable destination authentication during this probe.
    with tempfile.TemporaryDirectory(prefix="mts-host-key-") as temporary:
        captured = Path(temporary) / "known_hosts"
        probe = subprocess.run([*SSH, "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={captured}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "KnownHostsCommand=none", "-o", "VerifyHostKeyDNS=no",
            "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "UpdateHostKeys=no",
            "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "GSSAPIAuthentication=no",
            "-o", "HostbasedAuthentication=no", "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no", "-o", "PermitLocalCommand=no", host, "true"],
            capture_output=True, text=True, timeout=15)
        lines = {line for line in captured.read_text().splitlines() if line and not line.startswith("#")} if captured.exists() else set()
    if not lines:
        raise ValueError("Could not retrieve the host key: " + (probe.stderr.strip() or "No key received"))
    keys = {tuple(line.split()[1:]) for line in lines}
    if len(keys) != 1:
        raise ValueError("The address returned different host keys; contact the cluster administrator.")
    kind, encoded = keys.pop()
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(encoded, validate=True)).digest()).decode().rstrip("=")
    files = shlex.split(settings.get("userknownhostsfile", "~/.ssh/known_hosts"))
    if not files or files[0] == "none":
        raise ValueError("No writable user known-hosts file is configured")
    path = Path(files[0]).expanduser()
    if path == Path(os.devnull) or (path.exists() and not path.is_file()):
        raise ValueError("The configured known-hosts destination does not store host keys")
    keyhost = settings.get("hostkeyalias", hostname)
    lookup = keyhost if port == 22 else f"[{keyhost}]:{port}"
    return {"host": host, "hostname": hostname, "port": port, "fingerprint": fingerprint,
            "file": str(Path(files[0]).expanduser()), "lookup": lookup,
            "key": f"{lookup} {kind} {encoded}\n"}


def accept_key(host, proposal):
    # Do not accept an unseen key if DNS/configuration/key data changed after review.
    current = scan_key(host)
    if current != proposal:
        raise ValueError("The host key or connection settings changed. Check connections again.")
    path = Path(current["file"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(path.with_suffix(".mts-lock")):
        if path.exists():
            subprocess.run(["ssh-keygen", "-R", current["lookup"], "-f", str(path)],
                           capture_output=True, text=True, check=True, timeout=10)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write("\n" + current["key"])
    return {"host": host, "fingerprint": current["fingerprint"], "accepted": True}


def check_connections(site_file, log, proposals, *, host=None):
    profiles = load_sites(site_file, include_disabled=True)
    entries = connections(site_file)
    if host is not None and host not in {r["host"] for r in entries}:
        raise ValueError("Unknown SSH connection")
    selected = [r for r in entries if r["host"] == host] if host is not None else entries
    for row in selected:
        proposals.pop(row["host"], None)
    expected = deployment_expectation()
    def check(row):
        host = row["host"]
        if row["disabled"] and host_filter is None:
            return row | {"status": "Disabled", "detail": ""}
        log(f"Checking SSH access: {host}")
        try:
            active = [profiles[name] for name in row["profiles"]
                      if host_filter is not None or not profiles[name].disabled]
            directories = sorted({site.directory for site in active})
            command = "; ".join(f"test -d {shell_path(directory)} || printf '%s\\n' " +
                                shlex.quote("Missing working directory: " + directory) for directory in directories) or "true"
            result = subprocess.run([*SSH, host, command], capture_output=True, text=True, timeout=15)
            detail = result.stderr.strip()
            status, suggestion = "Connected", ""
            missing = [line for line in result.stdout.splitlines() if line.startswith("Missing working directory: ")]
            if not result.returncode and missing:
                status = "Connected"
                detail = "\n".join(missing)
                suggestion = "SSH login succeeded. The saved profile directory does not exist. Edit the profile or deploy the manager to create its working directory."
            repair = False
            address = {}
            if result.returncode:
                status = "Failed"
                if "REMOTE HOST IDENTIFICATION HAS CHANGED" in detail or "Host key verification failed" in detail:
                    repair = True
                    suggestion = "Accept the replacement host key to update the saved connection identity."
                    try:
                        proposals[host] = scan_key(host)
                        repair = True
                    except (ValueError, subprocess.SubprocessError) as error:
                        suggestion = str(error) + " Retry host-key retrieval to continue."
                elif "Could not resolve hostname" in detail:
                    suggestion = "Set the full login hostname and account name for this connection."
                    settings = effective(host)
                    address = {"edit_address": True, "hostname": settings["hostname"],
                               "user": settings["user"], "port": int(settings["port"])}
                elif "Permission denied" in detail:
                    suggestion = "Install an SSH public key using the account password."
                else:
                    suggestion = "Verify the server address, network access and SSH account settings."
            if status == "Connected" and not missing:
                detail = ""
            checked = row | address | {"profile_warning": bool(missing), "status": status, "detail": detail, "suggestion": suggestion,
                               "repair": repair, "fingerprint": proposals.get(host, {}).get("fingerprint")}
            if status == "Connected" and active:
                try:
                    checked["deployment"] = check_deployment(active, expected)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
                    checked["deployment"] = {"state": "unknown", "detail": detail}
                log(f"{host}: deployment {checked['deployment']['state']}")
            log(f"{host}: {status}" + ("\n" + detail if detail else ""))
            return checked
        except (OSError, subprocess.SubprocessError) as error:
            log(f"{host}: {error}")
            return row | {"status": "Failed", "detail": str(error)}
    host_filter = host
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(selected)))) as pool:
        rows = list(pool.map(check, selected))
    return {"connections": rows, "errors": {r["host"]: r["detail"] for r in rows if r["status"] not in {"Connected", "Disabled"}}}


def add_connection(site_file, data):
    name, hostname, user = (data[k].strip() for k in ("name", "hostname", "user"))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise ValueError("Use a connection name containing letters, digits, underscores or hyphens")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", hostname) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        raise ValueError("Invalid hostname or account name")
    port = int(data.get("port", 22))
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    site = Site(name=name, ssh=name, directory=data["directory"].strip(),
                source=data["source"].strip(), output_root=data["output_root"].strip(),
                executable="build/current/MTS2D", mode=data.get("mode", "slurm"))
    if not all((site.directory, site.source, site.output_root)):
        raise ValueError("Working, source and output directories are required")
    config = Path.home() / ".ssh/config"
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(Path(site_file).with_suffix(".lock")), locked(config.with_suffix(".mts-lock")):
        if name in load_sites(site_file, include_disabled=True):
            raise ValueError("A site with this name already exists")
        before = config.read_text() if config.exists() else ""
        # Generic Host defaults remain applicable after the new explicit entry.
        for line in before.splitlines():
            parts = shlex.split(line, comments=True)
            if parts and parts[0].lower() == "host" and any(name.lower() == p.lower() for p in parts[1:]):
                raise ValueError("An SSH Host entry already uses this name; configure its site profile in sites.toml")
        entry = f"Host {name}\n    HostName {hostname}\n    User {user}\n    Port {port}\n"
        # Prepend so later Match/Host sections cannot capture this entry.
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(entry + "\nHost *\n" + before)
        fields = {"ssh": name, "directory": site.directory, "source": site.source,
                  "output_root": site.output_root, "executable": site.executable, "mode": site.mode}
        with Path(site_file).open("a") as stream:
            stream.write(f"\n[sites.{name}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items()))
    return {"added": name}


def install_key(host, password, log):
    if not isinstance(password, str) or not password or any(c in password for c in "\r\n\0"):
        raise ValueError("A single-line account password is required")
    folder = Path.home() / ".ssh"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = folder / "mts_ed25519"
    if not key.exists():
        if key.with_suffix(".pub").exists():
            raise ValueError("An orphaned mts_ed25519.pub exists; inspect it before generating a key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "MTS"],
                       check=True, capture_output=True, text=True, timeout=15)
    public = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)],
                            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    # Only public key material enters the remote command. The password stays in a
    # private temporary file, read by OpenSSH's askpass helper; no shell receives it.
    quoted = shlex.quote(public)
    command = ('umask 077; mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && '
               'touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys" && '
               f'(grep -qxF -- {quoted} "$HOME/.ssh/authorized_keys" || '
               f'printf "\\n%s\\n" {quoted} >> "$HOME/.ssh/authorized_keys")')
    log(f"Installing public key on {host}; password authentication")
    with tempfile.TemporaryDirectory(prefix="mts-ssh-") as temporary:
        secret = Path(temporary) / "password"
        descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(password)
        helper = Path(temporary) / "askpass"
        helper.write_text('#!/bin/sh\ncat "$MTS_SSH_PASSWORD_FILE"\n')
        helper.chmod(0o700)
        env = os.environ | {"SSH_ASKPASS": str(helper), "SSH_ASKPASS_REQUIRE": "force",
                            "DISPLAY": "mts:0", "MTS_SSH_PASSWORD_FILE": str(secret)}
        result = subprocess.run(["ssh", "-T", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
                                 "-o", "ControlPath=none", "-o", "ControlMaster=no",
                                 "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive,password",
                                 "-o", "NumberOfPasswordPrompts=1", host, command],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                start_new_session=True, timeout=45)
    if result.returncode:
        raise ValueError(result.stderr.replace(password, "[redacted]").strip() or "Password authentication failed")
    # Register this identity for subsequent ordinary manager connections.
    config = folder / "config"
    with locked(config.with_suffix(".mts-lock")):
        previous = config.read_text() if config.exists() else ""
        identity_host = host.rsplit("@", 1)[-1]
        entry = f"Host {identity_host}\n    IdentityFile {json.dumps(str(key), ensure_ascii=False)}\n\nHost *\n"
        if entry not in previous:
            descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(entry + previous)
    verified = subprocess.run([*SSH, "-i", str(key), "-o", "IdentitiesOnly=yes", host, "true"],
                              capture_output=True, text=True, timeout=15)
    if verified.returncode:
        raise ValueError("Public key uploaded, but login verification failed: " + verified.stderr.strip())
    log(f"{host}: public key installed and login verified")
    return {"installed": host}


def configure_address(host, data):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", host):
        raise ValueError("Address editing requires an SSH alias")
    hostname, user = (data[k].strip() for k in ("hostname", "user"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", hostname) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
        raise ValueError("Invalid hostname or account name")
    port = int(data.get("port", 22))
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    config = Path.home() / ".ssh/config"
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    start, end = f"# MTS address: {host}\n", f"# End MTS address: {host}\n"
    with locked(config.with_suffix(".mts-lock")):
        previous = config.read_text() if config.exists() else ""
        if start in previous:
            first = previous.index(start)
            last = previous.index(end, first) + len(end)
            previous = previous[:first] + previous[last:]
        options = []
        for key in ("identityfile", "proxyjump", "proxycommand", "requesttty", "stricthostkeychecking", "userknownhostsfile"):
            value = data.get(key)
            values = value if isinstance(value, list) else [value]
            for value in values:
                if value:
                    if any(char in str(value) for char in "\n\r\0"):
                        raise ValueError("SSH settings must contain one value per line")
                    rendered = json.dumps(value, ensure_ascii=False) if key == "identityfile" else value
                    options.append(f"    {key} {rendered}")
        extra = data.get("extra", "")
        for line in extra.splitlines():
            parts = shlex.split(line, comments=True)
            directive = re.split(r"[\s=]", line.lstrip(), maxsplit=1)[0].lower()
            if parts and directive in {"host", "match", "include"}:
                raise ValueError("Additional directives must apply to this host only")
        entry = (start + f"Host {host}\n    HostName {hostname}\n    User {user}\n    Port {port}\n"
                 + "\n".join(options) + "\n" + extra + "\nHost *\n" + end)
        candidate = entry + previous
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as stream:
            stream.write(candidate)
            preview = Path(stream.name)
        try:
            validation = subprocess.run(["ssh", "-G", "-F", str(preview), host], capture_output=True, text=True, timeout=10)
            if validation.returncode:
                raise ValueError(validation.stderr.strip())
        finally:
            preview.unlink()
        config.with_suffix(".mts-backup").write_text(config.read_text() if config.exists() else "")
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(candidate)
    return {"configured": host}

def editor_settings(site_file, host):
    import tomllib
    from dataclasses import asdict
    raw = tomllib.loads(Path(site_file).read_text())["sites"]
    profiles = [{"name": name, "settings": {k: v for k, v in asdict(Site(name=name, **values)).items()
                                           if k not in {"name", "ssh"}}}
                for name, values in raw.items() if values.get("ssh") == host]
    settings = effective(host)
    keys = {"hostname": "hostname", "user": "user", "port": "port", "proxyjump": "proxyjump",
            "proxycommand": "proxycommand", "requesttty": "requesttty",
            "stricthostkeychecking": "stricthostkeychecking", "userknownhostsfile": "userknownhostsfile"}
    connection = {key: settings.get(value, "") for key, value in keys.items()}
    connection["identityfile"] = settings.get("identityfile", [])
    connection["extra"] = ""
    config = Path.home() / ".ssh/config"
    text = config.read_text() if config.exists() else ""
    start, end = f"# MTS address: {host}\n", f"# End MTS address: {host}\n"
    if start in text:
        block = text.split(start, 1)[1].split(end, 1)[0]
        connection["extra"] = "\n".join(line for line in block.splitlines()
            if line.strip() and line.split()[0].lower() not in
            {"host", "hostname", "user", "port", "identityfile", "proxyjump", "proxycommand",
             "requesttty", "stricthostkeychecking", "userknownhostsfile"})
    defaults = {k: v for k, v in asdict(Site(name="default", ssh=host,
        directory="~/simulation/MTS", source="MTS2D", executable="build/current/MTS2D",
        output_root="output", mode="detached")).items() if k not in {"name", "ssh"}}
    base = re.sub(r"[^A-Za-z0-9_-]", "_", host)
    if not re.match(r"[A-Za-z]", base):
        base = "site_" + base
    name, suffix = base, 2
    while name in raw:
        name = f"{base}_{suffix}"
        suffix += 1
    return {"host": host, "connection": connection, "effective": settings, "profiles": profiles,
            "default_profile": {"name": name, "settings": defaults}}


def _toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(value, allow_nan=False)


def save_profile(site_file, host, data):
    """Replace only the selected profile and validate the complete TOML before writing."""
    from dataclasses import asdict
    import tomllib
    name = data["name"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise ValueError("Profile names must start with a letter and contain letters, digits, underscores or hyphens")
    values = dict(data["settings"], ssh=host)
    site = Site(name=name, **values)
    if not site.directory.startswith(("~/", "/")) or not site.source or not site.output_root:
        raise ValueError("Use an absolute or ~/ working directory and specify source and output paths")
    path = Path(site_file)
    with locked(path.with_suffix(".lock")):
        original = path.read_text()
        raw = tomllib.loads(original)["sites"]
        editing = data.get("original_name")
        if editing:
            if editing != name or name not in raw or raw[name].get("ssh") != host:
                raise ValueError("The selected profile changed; reopen the editor")
        elif name in raw:
            raise ValueError("A profile with this name already exists")
        # Header spans retain all other profiles and their values. Full TOML
        # validation below rejects unsupported layouts rather than risking data loss.
        matches = list(re.finditer(r"(?m)^\s*\[[^\]\n]+\][ \t]*(?:#[^\n]*)?$", original))
        chunks = []
        cursor = 0
        for i, match in enumerate(matches):
            table = tomllib.loads(match.group().strip()).get("sites", {})
            finish = matches[i + 1].start() if i + 1 < len(matches) else len(original)
            if name in table:
                chunks.append(original[cursor:match.start()])
                cursor = finish
        chunks.append(original[cursor:])
        fields = asdict(site)
        fields.pop("name")
        replacement = f"\n[sites.{name}]\n" + "".join(f"{key} = {_toml_value(value)}\n" for key, value in fields.items())
        candidate = "".join(chunks).rstrip() + "\n" + replacement
        if tomllib.loads(candidate) != {"sites": raw | {name: fields}}:
            raise ValueError("Unsupported site-file layout; profile was not saved")
        backup = path.parent / ".state/sites-before-profile-edit.toml"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(original)
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as stream:
            stream.write(candidate)
            temporary = Path(stream.name)
        os.replace(temporary, path)
    return {"saved_profile": name}


def inspect_server(host, log, *, setup=""):
    """Inspect the login environment without creating directories or requiring a manager."""
    script = r'''set -e
printf 'MTS_INFO\thome\t%s\n' "$HOME"
printf 'MTS_INFO\tos\t%s\n' "$(uname -s)"
printf 'MTS_INFO\tcpu\t%s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf unknown)"
if command -v sbatch >/dev/null 2>&1; then printf 'MTS_INFO\tslurm\tyes\n'; else printf 'MTS_INFO\tslurm\tno\n'; fi
for candidate in python3 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if version=$("$candidate" -c 'import sys; print(".".join(map(str, sys.version_info[:3]))); sys.exit(sys.version_info < (3,11))' 2>/dev/null); then
            printf 'MTS_INFO\tpython\t%s\n' "$(command -v "$candidate")"
            printf 'MTS_INFO\tpython_version\t%s\n' "$version"
            MTS_INSPECT_PYTHON="$(command -v "$candidate")"
            break
        fi
    fi
done
for candidate in "$HOME"/simulation/MTS/MTS2D "$HOME"/simulation/MTS2D-*; do
    if test -f "$candidate/CMakeLists.txt"; then printf 'MTS_INFO\tsource\t%s\n' "$candidate"; fi
done
if test -d "$HOME/simulation/MTS"; then printf 'MTS_INFO\tworkspace_exists\tyes\n'; fi
if test -n "${MTS_INSPECT_PYTHON:-}"; then
    "$MTS_INSPECT_PYTHON" - <<'MTS_INSPECT_BUILDS'
import json, shutil, subprocess
from pathlib import Path
root = Path.home() / "simulation/MTS/executables"
manifests = sorted(root.glob("*/build-manifest.json"))
builds = []
for manifest in manifests[:128]:
    record = json.loads(manifest.read_text())
    builds.append({"path": str(manifest.parent / "MTS2D"), "revision": record["revision"],
                   "sha256": record["executable_sha256"], "built_at": record.get("built_at", "")})
print("MTS_INFO\tretained_builds\t" + json.dumps(builds))
print("MTS_INFO\tretained_build_count\t" + str(len(manifests)))
partitions = []
if shutil.which("sinfo"):
    try:
        result = subprocess.run(["sinfo", "--noheader", "--format=%P"], capture_output=True, text=True, check=True, timeout=5)
        partitions = sorted({name.rstrip("*") for name in result.stdout.split()})
    except (OSError, subprocess.SubprocessError):
        pass
print("MTS_INFO\tpartitions\t" + json.dumps(partitions))
MTS_INSPECT_BUILDS
fi
'''
    log(f"Inspecting {host}: login environment, Python, scheduler and source directories")
    result = subprocess.run([*SSH, host, "bash -l -s"], input=setup + "\n" + script,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Server inspection failed")
    info, sources = {}, []
    for line in result.stdout.splitlines():
        if line.startswith("MTS_INFO\t"):
            _, key, value = line.split("\t", 2)
            if key == "source":
                sources.append(value)
            elif key in {"retained_builds", "partitions"}:
                info[key] = json.loads(value)
            else:
                info[key] = value
    if not info.get("home", "").startswith("/"):
        raise ValueError("The server did not report an absolute home directory")
    suggestions = {"directory": info["home"] + "/simulation/MTS",
                   "source": "MTS2D",
                   "output_root": "output", "state_dir": ".state", "management_module": "MTS_Management",
                   "manager_directory": "", "executable": "build/current/MTS2D", "mode": "slurm" if info.get("slurm") == "yes" else "detached"}
    if info.get("python"):
        suggestions["python"] = info["python"]
    notes = [f"{info.get('os', 'Unknown OS')}; {info.get('cpu', 'unknown')} logical CPUs.",
             "Slurm is available." if info.get("slurm") == "yes" else "Slurm was not found in the login environment; detached execution is suggested.",
             "Python " + info["python_version"] if info.get("python") else "Python 3.11 or newer was not found. Configure a Python module or interpreter before deployment."]
    if not sources:
        notes.append("No simulator source was found. Upload MTS2D into ~/simulation/MTS/MTS2D before building simulations.")
    elif len(sources) > 1:
        notes.append("Existing source locations are listed below. The default active source is ~/simulation/MTS/MTS2D.")
    if int(info.get("retained_build_count", "0")) > 128:
        notes.append("The retained-build list shows the first 128 entries.")
    if info.get("slurm") == "yes" and not info.get("partitions"):
        notes.append("Partitions could not be determined; enter the partition name in the profile.")
    return {"inspected_host": host, "information": info | {"sources": sources},
            "suggestions": suggestions, "notes": notes}
