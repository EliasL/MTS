"""Setup operations shared by the command-line bootstrap and the browser."""

import argparse
import hashlib
import html
import importlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shlex
import shutil
import site
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".mts"
COMPONENTS = ("MTS2D", "MTSManager", "MTSPlot")


def run(command, log=print, *, env=None, json_result=False):
    log("$ " + shlex.join(map(str, command)))
    def forward(stream):
        for line in stream:
            log(line.rstrip())
    with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE if json_result else subprocess.STDOUT,
                          text=True, bufsize=1) as process:
        output = None
        if json_result:
            worker = threading.Thread(target=forward, args=(process.stderr,))
            worker.start()
            output = process.stdout.read()
            worker.join()
        else:
            forward(process.stdout)
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command, output=output)
        if json_result:
            return json.loads(output)


def synchronize_components(log):
    missing = [name for name in COMPONENTS if not (ROOT / name / (
        "CMakeLists.txt" if name == "MTS2D" else "pyproject.toml")).exists()]
    if missing:
        if not (ROOT / ".git").exists():
            raise RuntimeError("Use a Git clone of MTS; ZIP installation is not supported yet.")
        run(["git", "submodule", "update", "--init", "--recursive", "--", *missing], log)
    else:
        log("Component sources are present; existing checkouts and local edits are preserved.")


def install_packages(log):
    synchronize_components(log)
    saved = STATE / "uv-path"
    uv = os.environ.get("MTS_UV") or (saved.read_text().strip() if saved.exists() else None)
    uv = uv or shutil.which("uv") or str(STATE / "tools/uv")
    log("Installing the recorded Python package versions…")
    run([uv, "sync", "--quiet", "--locked", "--inexact", "--python", sys.executable,
         "--config-setting", "editable_mode=compat"], log)
    for directory in site.getsitepackages():
        site.addsitedir(directory)  # Load editable links installed after this server started.
    importlib.invalidate_caches()
    generate_launcher()
    fingerprint = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    (STATE / "installation.json").write_text(json.dumps({
        "workspace": str(ROOT), "lock_sha256": fingerprint,
        "python": sys.executable, "launcher": launcher_name(),
    }, indent=2) + "\n")
    log("Python packages and the desktop launcher are ready.")


def launcher_name():
    return "MTS.app" if platform.system() == "Darwin" else "MTS.desktop"


def generate_launcher():
    if platform.system() == "Darwin":
        contents = ROOT / "MTS.app/Contents"
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleName": "MTS", "CFBundleDisplayName": "MTS",
            "CFBundleIdentifier": "org.mts.local-launcher", "CFBundleVersion": "1",
            "CFBundlePackageType": "APPL", "CFBundleExecutable": "MTS", "LSUIElement": True,
        }))
        script = contents / "MacOS/MTS"
        script.write_text('''#!/bin/bash
MTS_ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)"
if ! /bin/bash "$MTS_ROOT/setup.sh" --start >> "$MTS_ROOT/.mts/launcher.log" 2>&1; then
    /usr/bin/osascript - "$MTS_ROOT" <<'APPLESCRIPT'
on run argv
    display alert "MTS could not start" message ("Open InstallationGuide.html in " & item 1 of argv & ". Startup details are in .mts/launcher.log.") as warning
end run
APPLESCRIPT
fi
''')
        script.chmod(0o755)
    elif platform.system() == "Linux":
        # Desktop Exec is not shell syntax. Escape its reserved characters directly.
        quote = lambda value: '"' + ''.join(('\\' + c) if c in '\\"`$' else c
                                           for c in str(value)).replace('%', '%%') + '"'
        desktop = ROOT / "MTS.desktop"
        desktop.write_text("[Desktop Entry]\nType=Application\nName=MTS\n"
                           "Comment=Open the local MTS simulation workspace\n"
                           f"Exec=/bin/bash {quote(ROOT / 'setup.sh')} --start\n"
                           f"Path={ROOT}\nTerminal=false\nCategories=Science;\n")
        desktop.chmod(0o755)
    else:
        raise RuntimeError("MTS currently supports macOS and Linux.")


def write_entry(url=None):
    """A local HTML entry page; it never attempts to execute programs in a browser."""
    command = html.escape(shlex.join(["bash", str(ROOT / "setup.sh"), "--start"]))
    link = (f'<a class="button" href="{html.escape(url, quote=True)}">Open MTS</a>'
            if url else '<p>MTS is not running. Open the desktop launcher to start it.</p>')
    entry = ROOT / "MTS.html"
    entry.write_text(f'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MTS</title>
<link rel="stylesheet" href="MTS_GUI/static/style.css"><main class="guide">
<a class="wordmark" href="InstallationGuide.html">MTS</a>
<small class="product-name">Mesoscopic Tensorial Simulation</small>
<h1>Session</h1>{link}
<p>Launch: <strong>{launcher_name()}</strong> or:</p><pre>{command}</pre>
<a href="InstallationGuide.html">Installation guide</a></main></html>''')
    entry.chmod(0o600)


def install_build_tools():
    """Explicit opt-in, in the invoking terminal so OS prompts remain visible."""
    system = platform.system()
    if system == "Darwin":
        if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode:
            subprocess.run(["xcode-select", "--install"], check=True)
            raise RuntimeError("Finish Apple's installation dialog, then run setup.sh with the same options again.")
        brew = shutil.which("brew")
        if not brew:
            raise RuntimeError("Install Homebrew from https://brew.sh, then rerun setup. "
                               "The installer does not install a system package manager automatically.")
        run([brew, "install", "cmake", "libomp"])
    elif system == "Linux":
        values = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                values[k] = v.strip('"')
        if values.get("ID") not in {"ubuntu", "debian"}:
            raise RuntimeError("Automatic build-tool installation currently supports Ubuntu/Debian. "
                               "Install a C++17 compiler, CMake, OpenMP and zlib development files, "
                               "then use setup.sh --local-sim.")
        run(["sudo", "apt-get", "update"])
        run(["sudo", "apt-get", "install", "build-essential", "cmake", "zlib1g-dev"])
    else:
        raise RuntimeError("MTS currently supports macOS and Linux.")


def build_simulator(log):
    if not shutil.which("cmake"):
        raise RuntimeError("CMake is missing. See InstallationGuide.html for optional compiler setup.")
    env = os.environ.copy()
    if platform.system() == "Darwin" and shutil.which("brew"):
        result = subprocess.run(["brew", "--prefix", "libomp"], capture_output=True, text=True)
        if result.returncode == 0:
            prefix = result.stdout.strip()
            env["CMAKE_PREFIX_PATH"] = prefix + os.pathsep + env.get("CMAKE_PREFIX_PATH", "")
            env["CPATH"] = prefix + "/include" + os.pathsep + env.get("CPATH", "")
            env["LIBRARY_PATH"] = prefix + "/lib" + os.pathsep + env.get("LIBRARY_PATH", "")
    run([sys.executable, "-m", "MTS_Management", "--sites", str(ROOT / "MTSManager/sites.toml"),
         "build", "--site", "local"], log, env=env, json_result=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-tools", action="store_true", required=True)
    parser.parse_args()
    install_build_tools()
