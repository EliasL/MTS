"""Start or reopen one workspace's local MTS server."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
import webbrowser

from .install import ROOT, STATE


def existing_session():
    record = STATE / "server.json"
    if not record.exists():
        return None
    saved = json.loads(record.read_text())
    parts = urlsplit(saved["url"])
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or saved["workspace"] != str(ROOT):
        raise RuntimeError("Invalid saved MTS session. Inspect .mts/server.json.")
    try:
        with urlopen(Request(f"http://{parts.netloc}/api/state",
                             headers={"X-MTS-Token": parts.fragment}), timeout=2) as response:
            state = json.load(response)
            if state["workspace"] != str(ROOT):
                raise RuntimeError("The saved address belongs to another MTS workspace.")
        return saved["url"]
    except (ConnectionError, TimeoutError, OSError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--local-sim", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    if args.serve:
        from .server import serve
        with (STATE / "server.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            serve(args.install, args.local_sim)
        return
    with (STATE / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        url = existing_session()
        if url is None:
            command = [sys.executable, "-m", "MTS_GUI", "--serve"]
            if args.install:
                command.append("--install")
            if args.local_sim:
                command.append("--local-sim")
            env = os.environ | {"MPLBACKEND": "Agg", "MPLCONFIGDIR": str(STATE / "matplotlib"),
                                "UV_CACHE_DIR": str(STATE / "cache"),
                                "UV_PYTHON_INSTALL_DIR": str(STATE / "python")}
            with (STATE / "server.log").open("a") as log:
                child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(100):
                url = existing_session()
                if url:
                    break
                if child.poll() is not None:
                    raise RuntimeError("MTS could not start. See .mts/server.log.")
                time.sleep(.1)
            if url is None:
                raise RuntimeError("MTS startup timed out. See .mts/server.log.")
        elif args.install:
            raise RuntimeError("MTS is already running. Use its Setup page, or Quit MTS before running setup again.")
    print("MTS is ready: " + url)
    if not args.no_browser:
        webbrowser.open(url)


if __name__ == "__main__":
    main()
