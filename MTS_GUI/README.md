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

Restart MTS and Quit MTS are separate buttons in the top bar.
Restart reloads the Python backend and then the browser page at the same local
address. Quit and Restart are disabled while a GUI operation is running.
Simulation workers continue independently; inventory snapshots remain on disk.
Backend operation history is cleared when the backend restarts.
The browser restores its selected panel after restart or reload. Changing panels
does not add terminal messages.

`install.py` handles component retrieval, package setup and launcher generation.
`server.py` exposes the supported operations; `__main__.py` manages the on-demand
server lifecycle. The buildless interface is in `static/`. Long operations run in
background tasks, and plotting runs in a separate process. Simulation workers
remain independent of the GUI process.

Start simulations configures and launches runs. Monitor data displays records
for a server or a campaign. Plot exports timing figures.

Both simulation panels include help on configuration hashes, prepared folders,
retained binaries, saved commands, simulator-owned resume and job chains. The
local GUI communicates with the manager installed on each cluster. Folder-based
jobs run their saved binary command independently of the manager's Python code.
The October 2026 benchmark retains its historical worker invocation. Campaign
metadata records missing settings and retained binary paths without changing
original run hashes.

SSH connections lists remote destinations and their site profiles. Registered
SSH accounts without a simulation profile support connection checks and key
installation; simulation operations require a site profile. Each row provides
SSH settings, Add profile or Edit profiles, manager deployment and connection
enabling. Site names are displayed in uppercase; stored SSH aliases and profile
identifiers retain their original spelling. A green dot denotes successful SSH
access without profile warnings. Diagnostics appear beneath the row.

Check connections checks enabled destinations concurrently using threads. Each
row also has Check connection, which can check a disabled destination. Checks
compare the deployed manager, simulator source record and campaign definitions
with this installation. Deploy is grey and disabled until verified, pale green
and disabled when current, red when an update is needed, and white when absent.
Deployment updates source and manager files; compilation remains separate and
retained executable selection is preserved.

The profile editor exposes paths, Python, execution mode and environment setup.
Slurm mode also exposes wall time and partition. Inspect server supplies partition
suggestions when available; the partition field also accepts a typed name. Other
manager, scheduler and resource settings are editable as JSON. Inspect
server reads the login environment, CPU count, Python versions, Slurm availability
and current source candidates under `~/simulation/MTS/MTS2D` and older named snapshots. Use suggested profile
settings fills the editor; Undo and Redo navigate successive edits, including
suggestions. Revert profile restores the last saved values; Save profile writes
the reviewed settings. Suggestions preserve populated storage and executable
paths in existing profiles. Inspection
does not create directories or require a remote manager. Existing profiles can
be selected individually. The site selector changes the SSH destination; New
profile starts a profile with default settings. The state directory is included
in the advanced JSON settings. Inspection lists retained builds as executable
choices, labelled by revision, date and binary hash.

SSH settings edits the login address, account, port, identities, proxy route,
terminal and host-key settings. Additional SSH directives apply to that alias;
all effective OpenSSH settings are displayed for inspection. Changes are validated
before saving and the previous SSH configuration is backed up.

A profile working directory is a configured simulation workspace, not an SSH
requirement. New profiles suggest `~/simulation/MTS`. Deploy manager creates the
chosen working directory and installs the shared manager independently of any
previous installation. The standard workspace is `~/simulation/MTS`, with the
active source in `MTS2D`, the working executable in `build/current/MTS2D`, and
retained executables in `executables/`. The separate `~/simulation/MTS2D` tree
is reserved for existing installations. Missing simulator sources must be configured or uploaded
before building simulations. Check
connections tests SSH login and working-directory availability. Connection
failures include the OpenSSH diagnostic and a suggested action. A changed or
unknown host key displays its ED25519 fingerprint with an Accept host key
action. If key retrieval fails, Retry host-key retrieval repeats the probe. Acceptance backs up the existing
known-hosts file and replaces the destination's entry. Key retrieval uses the configured SSH route, including proxy connections.
Unresolved aliases provide fields for the login hostname, account and port.

Disable connection excludes all profiles for that SSH destination from normal
lists and manager operations. Enable connection restores them. These local
preferences are stored in `MTSManager/.state/ssh-connections.json`; site profiles
also accept `disabled = true` in `sites.toml`.

Add connection writes an alias to `~/.ssh/config` and a profile to
`MTSManager/sites.toml`. Configure the remote working, simulator source and output
directories, then check the connection. Install SSH key creates the dedicated
`~/.ssh/mts_ed25519` key without a passphrase if it does not exist, installs only
its public key in the remote account, and verifies key authentication. The
password is used for that operation and excluded from logs and browser storage.
The remote host key must be accepted first. Additional MFA prompts require manual
setup. Installing a user key does not issue an SSH certificate.

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
deleting simulation files. Update MTS on servers transfers the manager,
campaign directories and verified simulator source. Failures are reported per
server profile. Enabled SSH destinations update concurrently using threads.
Profiles sharing a destination update together and reuse source uploads for the
same workspace. A version check skips current profiles before transferring files.
Unchanged simulator source is skipped independently of manager and campaign
changes, avoiding source archive creation, transfer and installation.
Both individual Deploy and bulk updates refresh the connection/version display
after completion. Compilation is a separate operation.

The shared manager installation is selected by `manager_directory` (default
`~/.local/share/mts/manager/`). Active locations are recorded in
`MTSManager/.state/manager-deployments.json` and also used by CLI calls. Direct
jobs execute their saved binary commands independently of that installation.
The October 2026 benchmark's worker commands retain their required historical
installation. Updates preserve GUI snapshots, scientific output and retained
binary archives.

Campaign definitions are read from `MTSManager/campaigns/**/simulations.json`
and listed in Monitor data even when an inventory cannot be loaded.
Expected configurations are matched by their full configuration hash against
all cached servers and managed local downloads in
`MTSManager/.state/retrieved/*/*`. Absent configurations are marked Missing,
meaning absent from available records. A missing or stale server snapshot does
not establish absence on the server. Executable and manifest identities remain
separate; a configuration match alone does not establish executable equivalence.
Local copies are labelled and never expose controls for the original remote job.

Campaign Refresh checks enabled site profiles sequentially for matching
configuration hashes, including profiles without a cached inventory. Each profile
receives one batched request; unrelated runs are not refreshed. Results merge
with the local cache, retaining unrelated rows and each failed server's previous
snapshot. Campaign refresh times are recorded separately from full-server
refresh times. Selecting a source never starts SSH or downloads simulation output.

Abort interrupts inventory and log reads, preserving unfinished cached snapshots
and skipping remaining profiles. It does not stop simulation jobs. The local SSH
process is terminated; remote Python readers also have a time limit, but may
continue until that limit after disconnection. File-changing and submission
operations cannot be interrupted through Abort.

The Terminal panel reports operation arguments, manager calls or subprocess
commands, elapsed time and failure diagnostics. Manager JSON responses are used
internally; inventory reports a simulation count. Each operation has an expandable entry;
completed entries collapse when a later message is added. Switching panels
preserves the running operation's expanded view. Clear terminal removes completed
entries and preserves running entries and their live output. Entries remain
available until deleted, cleared or the backend is stopped. The terminal follows new output automatically. Copy all copies every retained
entry, including collapsed entries. Subprocess output appears as it arrives. The same timestamped messages are written to backend stdout,
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
