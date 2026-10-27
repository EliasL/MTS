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
Quit MTS clears the saved token; a new launch after quitting requires a new launcher link.
Blocked browser storage does not prevent access through the launcher link.

Hover over Quit MTS, or focus it with the keyboard, to reveal Restart MTS.
Restart reloads the Python backend and then the browser page at the same local
address. Quit and Restart are disabled while a GUI operation is running.
Simulation workers continue independently; inventory snapshots remain on disk.
Backend operation history is cleared when the backend restarts.

`install.py` handles component retrieval, package setup and launcher generation.
`server.py` exposes the supported operations; `__main__.py` manages the on-demand
server lifecycle. The buildless interface is in `static/`. Long operations run in
background tasks, and plotting runs in a separate process. Simulation workers
remain independent of the GUI process.

Start simulations configures and launches runs. Monitor data displays records
for a server or a campaign. Plot exports timing figures.

Inventory snapshots are stored in `.mts/inventory.sqlite3`, keyed by the complete
site configuration. Selecting a site reads its local snapshot without contacting
the execution host. Refresh updates it in a background task. The table remains
available during refresh; failed updates retain the previous snapshot and its
timestamp. The last successful update age is shown above the table. Profiles
sharing an SSH destination are grouped as one server; their output roots remain
distinct, and row actions use the original profile.

Execution hosts with the updated manager also maintain their own inventory
database. Refresh rereads unfinished runs and reuses completed records;
MTSManager invalidates a completed record when preparing another attempt.
Setup and tools contains a collapsed Advanced section. Hard reset inventory
rebuilds the selected server inventories, including finished runs, without
deleting simulation files. Update all server managers deploys the installed
local manager version once per SSH destination and verifies each profile before
activating it. Failures are reported per server profile in the terminal.

Manager updates replace the shared `~/.local/share/mts/manager/` installation;
they do not create a package copy per update. Submission and execution manager
identities are recorded for provenance without requiring an exact version match.
Executable, configuration and checkpoint checks remain enforced. Saved job
requests must remain compatible with manager updates. Other manager directories
referenced by existing submission scripts are left untouched. Active locations
are recorded in `MTSManager/.state/manager-deployments.json`; CLI calls using the
same site file also use these locations. Updates preserve GUI snapshots.

Campaign definitions are read from `MTSManager/campaigns/**/campaign.json`.
Expected configurations are matched by their full configuration hash against
all cached servers and managed local downloads in
`MTSManager/.state/retrieved/*/*`. Absent configurations are marked Missing,
meaning absent from available records. A missing or stale server snapshot does
not establish absence on the server. Executable and manifest identities remain
separate; a configuration match alone does not establish executable equivalence.
Local copies are labelled and never expose controls for the original remote job.

Campaign Refresh updates known source profiles sequentially, preserving each
successful result even if another source fails. A server inventory is needed to
discover campaign members on additional servers. Selecting a source never starts
SSH or downloads simulation output.

Abort interrupts inventory and log reads, preserving unfinished cached snapshots
and skipping remaining profiles. It does not stop simulation jobs. The local SSH
process is terminated; remote Python readers also have a time limit, but may
continue until that limit after disconnection. File-changing and submission
operations cannot be interrupted through Abort.

The Terminal panel reports operation arguments, manager calls or subprocess
commands, elapsed time and failure diagnostics. Manager JSON responses are used
internally; inventory reports a simulation count. Each operation has an expandable entry;
starting another operation collapses older entries. Entries remain available
until deleted, cleared or the backend is stopped. Subprocess output appears as
it arrives. The same timestamped messages are written to backend stdout,
which the desktop launcher redirects to `.mts/server.log`. To follow this output
in a terminal, run `tail -f .mts/server.log` from the MTS root.

Data-source selections and browser errors appear as terminal entries. Browser
entries survive page reloads in the same tab when session storage is available;
delete them individually, clear the terminal, or quit MTS to remove them.
An inventory without a saved snapshot displays “Refresh to update data overview.”

The session token, environment, generated figures and logs live in the ignored
`.mts/` directory or generated entry files. Do not expose the server to a network
or publish these generated files.

Run the interface tests from the MTS root:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

MTS2D, manager and plotting tests remain in their respective repositories.
