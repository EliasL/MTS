# MTS browser interface

The browser interface provides setup, local and remote simulation controls, and
timing-figure export. It uses the MTSManager and MTSPlot Python packages for
scientific operations. The HTTP layer uses Python's standard library and runs
only on the loopback interface.

Run `bash setup.sh` from the parent MTS folder to install dependencies and create
a desktop launcher. `bash setup.sh --start` reopens the same workspace without
installing packages. See [InstallationGuide.html](../InstallationGuide.html) for
optional simulator compilation.

The browser remembers a validated session token in local storage for its local
address, allowing tabs to be closed and reopened while the backend is running.
Quit MTS clears the saved token; a restarted server requires a new launcher link.
Blocked browser storage does not prevent access through the launcher link.

`install.py` handles component retrieval, package setup and launcher generation.
`server.py` exposes the supported operations; `__main__.py` manages the on-demand
server lifecycle. The buildless interface is in `static/`. Long operations run in
background tasks, and plotting runs in a separate process. Simulation workers
remain independent of the GUI process.

The session token, environment, generated figures and logs live in the ignored
`.mts/` directory or generated entry files. Do not expose the server to a network
or publish these generated files.

Run the interface tests from the MTS root:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

MTS2D, manager and plotting tests remain in their respective repositories.
