# MTS browser interface

MTS provides a local browser interface for configuring and monitoring
simulations, inspecting scientific results and exporting figures. Install and
open it from the workspace root using the [installation guide](../InstallationGuide.html).

## Local data output

In **Overview → Local data output path**, select a local site and enter an
empty destination folder. Use **Move current output** to transfer the site's
runs and archives. Use **Create local site** to keep the existing output and
add a separate destination for future runs. Local sites share the simulator
build of the site they were created from.

If a move affects running simulations, review their checkpoints and choose
**Stop, move and resume**. The new attempts resume from their latest saved
checkpoints, repeating any later steps. Wait for runs without usable
checkpoints or create a separate site. Selecting a drive root proposes an
`MTS` folder on that drive.

## Start a simulation

In **Start simulations**, select an experiment and execution site. Set the mesh
size, loading schedule and output options. Save the settings as a preset to
reuse them, or import a native MTS2D `.conf` file. **Start simulation** launches
one run. Local individual runs continue when the GUI closes.

- **Simple shear** applies a shear transformation. **Axial compression** and
  **tension** support affine loading or displacement of a driven boundary.
  For vertical boundary compression, drive the top, fix the bottom, and leave
  the sides free. Displacement uses mesh length units.
- **Nanoindentation** uses a circular indenter with bottom fixation and free
  sides. Plot vertical reaction force against load for a force–depth curve.
- **Sequential slip** drives selected edge regions. For example, enter
  `1.5,2.5,2.5` for three full slips at the midpoint of the selected edges.
- Select `contiTriangular` for a staggered triangular mesh. Boundary
  displacement supports top and bottom loading on this lattice.
- Select **Conti2Phase (experimental)** under **Additional settings → Energy
  density function** for a square initial sample with equal-depth square and
  triangular wells. See [Scientific issues](../docs/TheSimulationProcess.md#scientific-issues).

Use **Custom mesh** to preview the sample on the selected execution site.
Click to remove triangles and Shift-click to restore material from the original
lattice. Download `initialMesh.vtu` to save the edited mesh separately. An
individual run stores its edited mesh in the run folder.

To create a campaign, select a saved preset and add sweep parameters. Linear and
logarithmic sweeps include both endpoints. Custom values can define specific
seeds or settings. Save the campaign, review its size and execution site, then
launch it. Completed members are skipped and other folder conflicts are
reported individually. Use a separate output root for independent repetitions
with identical settings.

## Monitor and retrieve results

**Monitor data** shows cached records by site or campaign. For a remote site,
**Refresh** retrieves a newer completed inventory or requests a scan and
reports an estimated wait. Click **Refresh** again to check progress or
retrieve the result. **Last update** shows when the displayed inventory was
generated. **Hard reset inventory** rereads finished simulations and rebuilds
the full run index. Scheduler state and scientific completion are displayed from
separate records.

**Runtime** and **ETR** show the last recorded simulation runtime and estimated
time remaining. Click a column heading to sort within the displayed groups.
Right-click a heading or choose **Columns** to show or hide columns. Drag a
heading to change its position. The browser saves column preferences.

Select a simulation to plot its local macro data. For a remote simulation, MTS
retrieves the macro table and run metadata. Compatible runs that differ only by
seed can be included in the plot.

- **Use as template** loads a run's settings and initial mesh for a fresh run.
- **Load further** creates a new run folder using a checkpoint from the source.
  It preserves the source run. **Resume** starts a new attempt in the original
  folder for a failed or stopped local run with a valid checkpoint. Failed
  remote runs also support resume.
- **Archive** moves an inactive run out of the active output root. **Archive
  log** shows archived runs and supports restoring them. **Remove all missing**
  clears records for folders that are already absent.

Use **Open simulation folder** to work with local files in place. **Plot** uses
`macroData.csv`, **Load settings** uses `config.conf`, and **Animate** requires
native VTU snapshots in `data/` plus the run configuration and manifest. Linux
folder browsing requires Zenity or KDialog. A full path can be entered directly.

## Plot and animate

Select a campaign and local data collection, or select a run in Monitor data.
For campaigns, a red **Refresh** button indicates missing or outdated CSVs
relative to the latest saved Monitor inventory. Click it to download those
files and reload the plot options. When no collection is selected, MTS
creates one for the campaign.

Plot a saved response column against load for a curve, or select a varied
campaign parameter on X for scalar comparisons. Assign other varied parameters
to colour or gradient, or hold them fixed. A scalar response can use the first,
last, mean, minimum or maximum of the saved rows. Timing summaries use completed
sampling targets and exclude diagnostic runs.

The seed mean weights each available seed equally and displays sample standard
deviation when available. With differing monotonic load grids, interpolation is
restricted to the common load interval. Select one seed to inspect repeated or
nonmonotonic loads on incompatible grids. Preview and PDF export use the same
plot specification. Named presets retain the axes, filters and appearance.

For **Animate**, select one run with saved mesh snapshots. Choose an element
field, playback schedule and display limits. **VTU files per unit load** in
the simulation settings controls the cadence of regular snapshots. For remote
runs, MTS retrieves the snapshots needed for rendering. MP4 requires FFmpeg
with H.264 encoding. MTS uses the system executable or the path in
`.mts/ffmpeg-path`.

By default, automatic colour limits come from the last saved mesh. Clear
**Range from last frame** to use the scheduled animation frames. Preview uses
the same colour limits as export. The preview frame menu selects the first,
middle or last frame. Animation presets store rendering and playback settings,
including the opacity curve.

## Connections and logs

Use **Setup and tools** to manage SSH connections, site profiles and builds.
Check connectivity and review the server profile before submission. **Update
MTS on servers** transfers the manager, campaign files and verified source.
Compile the simulator on the execution site when the selected build needs an
update.

**Slurm control** in the advanced tools holds, releases or cancels reviewed
queued jobs. **Review matching jobs** queries the live queue and displays the
matching IDs. **Run reviewed action** rechecks those jobs before proceeding.

The Terminal shows operation progress and diagnostics. **Abort** interrupts
supported reads and animation tasks. **Restart MTS** reloads the backend and
restores the browser draft where possible. **Quit MTS** ends the local GUI
session. Simulation workers have independent lifetimes. Logs are in
`.mts/server.log` and `.mts/launcher.log`.

## Development

The loopback HTTP server and task lifecycle are in `server.py`. Monitoring and
archive operations are in `monitor.py`. Plot sources, previews and exports
are in `plots.py`. Simulation forms use `simulation_setup.py`, and
`submission.py` adapts form and campaign selections to the
[manager API](../MTSManager/API.md). Browser assets are in `static/`.

See the [simulation process](../docs/TheSimulationProcess.md) for run records and
remote execution, and [testing](../tests/Testing.md) for verification commands.
