"""Start or reopen one workspace's local MTS server."""

import argparse
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
import webbrowser

from .install import ROOT, STATE


def check_server_lock():
    """Offer recovery only for a verified MTS process holding this workspace's lock."""
    with (STATE / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        lsof = shutil.which("lsof")
        holders = subprocess.run([lsof, "-t", str(STATE / "server.lock")],
                                 capture_output=True, text=True) if lsof else None
        pids = set(holders.stdout.split()) - {str(os.getpid())} if holders else set()
        pid = int(pids.pop()) if len(pids) == 1 else None
        command = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True) if pid else None
        if not command or "-m MTS_GUI --serve" not in command.stdout:
            raise RuntimeError("Another process holds .mts/server.lock, but its MTS identity could not be verified. "
                               "Inspect the lock with lsof .mts/server.lock. Do not delete the lock file.")
        message = (f"MTS process {pid} holds this workspace's server lock, but its session is not responding. "
                   "Quitting it may interrupt an active MTS operation.")
        if sys.platform != "darwin" or os.environ.get("MTS_DESKTOP") != "1":
            raise RuntimeError(message + f" Quit it with kill -TERM {pid}, then launch MTS again. "
                               "A computer restart is unnecessary.")
        answer = subprocess.run(["/usr/bin/osascript", "-", message], input='''on run argv
    display dialog (item 1 of argv) with title "MTS is already running" buttons {"Cancel", "Quit and Relaunch"} default button "Cancel" cancel button "Cancel" with icon caution
end run
''', text=True, capture_output=True)
        if answer.returncode:
            if "(-128)" in answer.stderr:
                raise SystemExit(0)  # Cancellation leaves the existing server untouched.
            raise RuntimeError("Could not display the recovery dialog: " + answer.stderr.strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                time.sleep(.1)
        raise RuntimeError(f"MTS process {pid} has not released its lock after SIGTERM. "
                           "Inspect it in Activity Monitor before trying again.")


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
    parser.add_argument("--restart", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    if args.serve:
        from .server import serve
        with (STATE / "server.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another MTS server holds .mts/server.lock. "
                                   "Reopen the desktop launcher to recover the existing process.") from None
            restart = serve(args.install, args.local_sim, args.restart)
        if restart:
            os.execv(sys.executable, [sys.executable, "-m", "MTS_GUI", "--serve", "--restart"])
        return
    with (STATE / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        url = existing_session()
        if url is None:
            check_server_lock()
            command = [sys.executable, "-m", "MTS_GUI", "--serve"]
            if args.install:
                command.append("--install")
            if args.local_sim:
                command.append("--local-sim")
            env = os.environ | {"MPLBACKEND": "Agg", "MPLCONFIGDIR": str(STATE / "matplotlib"),
                                "UV_CACHE_DIR": str(STATE / "cache"),
                                "UV_PYTHON_INSTALL_DIR": str(STATE / "python")}
            with (STATE / "server.log").open("a") as log:
                log_start = log.tell()
                child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(100):
                url = existing_session()
                if url:
                    break
                if child.poll() is not None:
                    details = (STATE / "server.log").read_bytes()[log_start:][-4000:].decode(errors="replace").strip()
                    raise RuntimeError(f"MTS could not start (exit {child.returncode}).\n{details}\n"
                                       f"Server log: {STATE / 'server.log'}")
                time.sleep(.1)
            if url is None:
                raise RuntimeError("MTS startup timed out. See .mts/server.log.")
        elif args.install:
            raise RuntimeError("MTS is already running. Use its Setup page, or Quit MTS before running setup again.")
    print("MTS is ready: " + url)
    if not args.no_browser:
        webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        sys.exit(str(error))
