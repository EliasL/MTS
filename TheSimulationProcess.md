# The simulation process: target design

This document specifies the target process for configuring, submitting,
executing, continuing and inspecting simulations. The folder-based MTS2D
interface, simulator-owned resume and locking, campaign file layout, metadata
resolution and combined server update described here require implementation.
Existing numerical procedures are retained unless explicitly changed below.

[TheSimulationProcess_old.md](TheSimulationProcess_old.md) records the existing
implementation. Source links in this document identify code to retain or modify;
they do not imply that a proposed interface already exists. Check implementation
status before using the example commands.

## 1. Components and terminology

| Component or record | Responsibility |
| --- | --- |
| MTS GUI | Creates configurations, invokes manager operations, updates servers and displays scheduler state, scientific output and local copies. |
| MTSManager | Validates submission inputs, prepares output folders and configuration files, retains binaries, submits final commands and locates results. |
| Slurm | Queues allocations, applies resource limits and explicit dependencies, starts the saved shell script and records scheduler state and process exit. |
| MTS2D | Owns folder-based startup, configuration discovery, resume/checkpoint selection, the execution lock, physical simulation, numerical recovery and scientific termination. |
| Configuration | Original saved simulator settings, excluding the generated name. These determine configuration identity. |
| Effective settings | Original settings supplemented for interpretation by campaign metadata where keys are absent. These do not replace original identity settings. |
| Run | A prepared output folder associated with a configuration hash and selected executable checksum. |
| Attempt | One submitted invocation of that run, with a saved command/request and its own log. |
| Campaign | A directory containing simulation definitions, resource requests and editable descriptive metadata. It does not pin a binary. |
| Retained build | A saved executable and its original build manifest, shared by any number of jobs. |
| Checkpoint or dump | A serialized physical simulation written and selected for continuation by MTS2D. |

Submission acceptance, process exit and scientific termination remain distinct.
A scheduler receipt means the job was accepted. A process can exit successfully
because a simulation is already complete. Scientific termination is established
by MTS2D output, including its termination reason and counters.

There is no Python manager worker between the queued shell script and MTS2D.
Routine monitoring reads records; it does not advance the simulation or select
its next checkpoint.

## 2. Files and identities

Site profiles determine absolute locations. The schematic paths below apply to
remote and local execution.

```text
MTSManager/
  sites.toml
  campaigns/<campaign-directory>/
    simulations.json                 # Campaign name and original run definitions
    resources.json                   # Resource plan and benchmark constraints
    metadata.json                    # Editable supplemental descriptions
  .state/
    job-log.jsonl                    # Submission and administrative events
    manager-deployments.json

execution-workspace/                 # Remote or local
  MTS2D/                             # Current source snapshot
  build/current/
    MTS2D
    build-manifest.json
  executables/<build-record-hash>/
    MTS2D
    build-manifest.json
    .retained

output-root/
  .prepare.lock
  .run-index.json
  .inventory.sqlite3
  <label>[<first-20-configuration-hash-digits>]/
    config.conf                      # Prepared before submission
    manifest.json
    .run.lock                        # Held by MTS2D during execution
    macroData.csv
    termination.json                 # Scientific termination summary
    data/
    dumps/
    backups/
    attempts/<attempt-id>/
      request.json                   # Saved command, binary, environment and mode
      submit.sh                      # Self-contained shell launch; no Python worker
      stdout.log                     # Output captured by scheduler/launcher
```

The campaign directory is the unit of definition and metadata distribution.
`simulations.json` retains the existing `name` and `runs` structure; renaming the
file does not change its run settings or configuration hashes. `resources.json`
retains applicable resource-plan contents. Different campaign directories can
have different resource requirements.

### 2.1 Configuration identity and its purpose

Configuration identity provides a deterministic mapping from settings to an
output folder. Each distinct set of identity settings receives a distinct hash,
so simulations with different settings are kept in separate folders. Given a
known configuration, the manager can calculate the hash and find the associated
folder without scanning every simulation's scientific output. Going in the
other direction requires no hash reversal: open the folder and read its
`config.conf`.

The identity remains SHA-256 of canonical JSON:

```json
{"format": "mtscripts-config-v1", "settings": {"...": "original saved settings"}}
```

Canonical serialization sorts keys, uses compact separators and rejects NaN.
For new simulations, all supported settings are explicit. Original saved values
participate, including thread count, backend and output settings. Labels,
campaign names, site paths, resource requests and supplemental metadata do not.

An existing simulation keeps its original identity representation when a new
setting is described through metadata. The manager does not insert that value
into the original configuration, rename its folder or silently recalculate its
identity from current defaults. A newly created configuration with the additional
explicit setting has its own identity, even if it describes equivalent physical
behaviour to an older configuration.

The folder name includes a readable label and the first 20 hash digits. The full
hash is retained in manifests and indexes. The index resolves existing labels;
changing the label is not a way to obtain another independent copy of identical
settings. Independent repetitions with the same settings use another output
root. Hash-prefix collisions raise an error. Labels retain the existing
1–120-character ASCII letter/digit/underscore/dot/hyphen constraints.

### 2.2 Other identities

| Record | Target use |
| --- | --- |
| Executable checksum | Verify retained bytes during preparation; associate the run and attempt with the selected binary. |
| Build-record hash | Name the shared retained copy. Multiple jobs reuse the same matching record and binary. |
| Source identity | Record the source revision and build-input digest when building. |
| Build schema | Describe the binary's build-time setting names; do not require equality with a newer manager's schema merely to use that binary. |
| Manager identity | Record submission/deployment provenance. No manager package is imported at queued execution time. |
| Checkpoint selection | MTS2D records which checkpoint was actually loaded; deferred `--resume` does not have a checkpoint checksum known at submission. |
| Manifest checksum | Compare downloaded and remote copies and reject a different destination run. |
| Campaign metadata | Editable interpretation of absent settings; no identity checksum or immutability requirement. |

The retained executable is assumed to remain at its saved path with unchanged
bytes. Its required shared libraries and execution environment must also be
available. The queued script records the environment setup and final command.

## 3. Configuration creation, historical settings and campaign files

Implementation references: [config.py](MTSManager/MTS_Management/config.py),
[defaults.json](MTSManager/MTS_Management/defaults.json),
[campaign.py](MTSManager/MTS_Management/campaign.py).

### 3.1 New configurations

`Config(**settings)` rejects unknown keys and fills absent keys from current
manager defaults. It converts integers supplied for floating-point settings to
floats, requires other exact default-derived Python types, rejects nonfinite
floats, and rejects newline, carriage return, `#` and `=` in string values.

`Config._validate()` then checks:

- Enumerated values for loading, deformation, boundary, reconnection, minimizer,
  preconditioner, tangent backend, preconditioner update, energy function,
  mesh diagonal and `stopWhen`.
- Positive `rows`, `cols`, `nrThreads` and `LBFGSNrCorrections`.
- Nonzero `loadIncrement`; nonnegative `totalLoad` and `segmentLoad`.
- Valid comma-separated signed horizontal/vertical directions. Multiple or
  repeated directions require positive `segmentLoad`.
- Direct displacement cannot use the fully periodic boundary setting.
- Nonnegative `startLoad`, a finite sum of starting load and load budget, and a
  positive budget large enough to advance floating-point load.
- Step noise requires relaxation. Simple shear without relaxation requires an
  affine initial guess.
- Positive reversibility tolerance; nonnegative energy-drop threshold,
  step-count targets and step-energy tolerance.
- Plastic/elastic sampling targets require relaxation, simple shear and the
  `contiSquare` energy function.
- Positive preconditioner displacement threshold.
- `forceReRun` is disallowed through the manager.
- Nonnegative checkpoint interval, maximum saved elastic reversibility states,
  VTU frame count, solver iteration limits, residual-force tolerance, disorder
  amplitude and noise amplitudes.

These checks do not establish that every solver parameter is scientifically
appropriate. Additional solver and mesh checks occur inside MTS2D.

The manager validates configurations before preparing and submitting them.
MTS2D validates its own inputs when it starts. These checks have different
purposes: submission validation catches errors before queueing; simulator
validation also protects direct invocations without the manager.

### 3.2 Existing saved configurations

Read original settings separately from their effective interpretation. Verify
the existing name/hash and manifest against the original settings, not a mapping
expanded with current defaults. Resolve known absent settings from the campaign
metadata when interpreting or validating historical records. An unresolved
required setting remains an error; an explicit simulation value wins over the
supplemental description.

Neither this resolution nor an inventory refresh rewrites `config.conf`.
MTS2D must also preserve the identity-bearing configuration instead of appending
a newly resolved `tangentBackend` to it. Resolved execution information belongs
in output records rather than in edits to the submitted settings.

### 3.3 The three campaign files

`simulations.json` contains the campaign name and original labelled settings:

```json
{
  "name": "MAGI-Benchmark-20261023",
  "runs": [
    {"label": "Example", "settings": {"...": "original simulation settings"}}
  ]
}
```

The example abbreviates a complete run definition. Duplicate configuration
hashes remain errors, even when labels differ. Campaign generation retains
Cartesian-product axes and the `L` convenience axis. Creating a new campaign
does not overwrite an existing definition.

`resources.json` holds resource requests and applicable benchmark constraints.
Resource validation can cover the planned simulation definitions, required
diagnostic runs and source revision. Any definition checksum excludes editable
`metadata.json`. Renaming the former files must preserve their run identities
and update all readers, launchers, examples and deployment paths.

`metadata.json` supplies campaign-wide interpretations of settings absent from
original run definitions:

```json
{
  "missing_settings": {
    "tangentBackend": "eigen"
  }
}
```

The containing campaign associates this file with the name in `simulations.json`.
The file can be edited over time. Its values are not hashed into configuration
identity, frozen after use or required to match an earlier checksum. Ordinary
JSON parsing and effective-setting type/value validation still apply. It does
not override an explicitly saved simulation setting.

Metadata describes historical behaviour; it is not a hidden replacement for
the selected executable's settings. The existing benchmark's retained Eigen
binary continues to implement Eigen behaviour without reading campaign metadata.
New simulations explicitly record their backend. Executing the same old folder
with an arbitrary newer binary is not an implicit consequence of updating MTS.

The manager, GUI and plotting readers use the same resolution rules where they
interpret historical settings. Parsing ordinary JSON reads the complete file;
the separate small metadata file avoids parsing every run merely to read those
descriptions. Load and cache it as needed, invalidating cached interpretations
when it changes.

## 4. GUI and CLI submission entry points

Implementation references: [app.js](MTS_GUI/static/app.js),
[server.py](MTS_GUI/server.py), [cli.py](MTSManager/MTS_Management/cli.py).

The GUI constructs settings from form fields and advanced overrides, validates
them through the manager and chooses an execution site. A GUI operation ending
successfully establishes submission acceptance, not scientific completion.
Campaign submission reads `simulations.json`, the resource plan and metadata.
Standalone submission reads its supplied configuration; unrelated campaign
metadata is not applied to it.

Before a job enters the queue, the manager prepares the output folder, config,
manifest, retained executable reference and attempt command. Preparation can
also be inspected before submission. It does not wait for a compute allocation
to create the run folder or discover that a required configuration is missing.

The target binary invocation accepts a run directory. Schematic examples are:

```sh
/retained/build/MTS2D /absolute/output/run
/retained/build/MTS2D /absolute/output/run --resume
```

These are proposed commands, not the current CLI. The exact positional-folder
syntax must be implemented together with the new `--resume` option. The first
form is a fresh launch; the second resumes an existing prepared run, starting
from its initial state if it has no checkpoint yet. This allows the same resume
command to be used for the first allocation and later allocations of a chain.

Local foreground execution invokes the binary directly and waits. Detached
execution and Slurm execute the saved command/script without `_execute` or a
Python import of MTSManager. The GUI can close after acceptance without owning
the simulator process lifetime. Batch preparation/submission failures retain
already accepted jobs and explicit receipts; they do not trigger blind retries.

## 5. Site selection and remote submission

Implementation reference: [cluster.py](MTSManager/MTS_Management/cluster.py).

Site profiles retain working/source/executable/output/state paths, execution
mode, environment setup and resource settings. Validate profile types, disabled
connections, SSH aliases, environment names and CPU requests before submission.
Resolve local paths locally and remote paths on their execution host.

SSH calls to the manager are submission-time or administrative operations.
They can prepare folders, verify builds, transfer source, submit scripts and
read inventories. They are not part of a queued job's runtime command.

Submission records the selected retained executable, original configuration
identity, run folder, attempt ID, environment, resume mode and requested
resources. The client records requested and confirmed submissions. An SSH
failure or timeout can leave the remote outcome unknown; inspect scheduler and
receipts before retrying. Acceptance is not assumed absent merely because the
client did not receive a reply.

Explicit scheduler dependencies serialize continuation allocations. A shared
job name is a label, not an execution dependency. CPU requests must accommodate
the configured thread count and the site's logical/physical CPU convention.

## 6. Source installation, builds and retained executables

Implementation references: [provenance.py](MTSManager/MTS_Management/provenance.py),
[deployments.py](MTSManager/MTS_Management/deployments.py).

### 6.1 Source identity and build preparation

The manager does not silently build when submitting a run. Builds are explicit.
For a Git source tree, build provenance requires main-branch identity or a
detached commit with verified `origin/main` ancestry, no tracked modifications
to build inputs, and no untracked files under `src` or `cmake`. The source
digest covers tracked CMake, `cmake`, `src` and `libs` inputs. An uploaded source
snapshot is verified against `SOURCE.json` and `SOURCE_FILES.json`.

The build procedure:

1. Locks `.source.lock`, shared with source installation.
2. Requires positive parallelism and a Boolean native-architecture choice.
3. Refuses to build inside a retained directory.
4. Rejects a CMake cache recording different source/build locations.
5. Retains the existing working executable before replacing it.
6. Computes source identity and requires the C++ parser's recognized setting
   names to match the current manager defaults.
7. Configures Release mode, compile-command export and native-architecture
   selection; builds target `MTS2D` with the requested parallelism.
8. Verifies that source identity did not change during the build.
9. Writes a build manifest containing revision, source hash, executable hash,
   setting-name hash, timestamp, build type, selected CMake options and cache hash.
10. Retains the new executable and manifest.

### 6.2 Retention and executable selection

`retain_executable()` checks that the recorded branch is `main` and that binary
bytes match the build manifest. A `.retained` marker returns that existing
verified path. Otherwise a lock protects the archive, whose destination is the
digest of the build record. An existing matching destination is reused.

A new retained copy is prepared in a temporary directory, copied and verified,
marked retained, and renamed into the archive. The executable mode is `0555`;
the manifest mode is `0444`. These modes discourage modification but are not a
permanent filesystem guarantee. Many jobs can use the same retained copy.

Retained executable selection verifies the saved binary checksum and original
build record. A different current-manager setting-name checksum is not itself
grounds for rejecting an older binary. Build-time source/schema agreement is
retained for building the current simulator. The manager must distinguish that
build-time check from historical executable selection.

There is no repeated manager version/schema check when the queued job begins,
because it invokes the binary directly. Updating working source or builds does
not change the saved command's retained executable path.

### 6.3 Deployment and update paths

`deploy(..., include_source=True)` can upload manager files and a verified
simulator-source archive. It excludes local state, output, builds, virtual
environments and Git metadata, and creates a native cluster profile only if
one does not exist. Source upload protects the separate legacy
`~/simulation/MTS2D` location.

Source installation requires a managed snapshot rather than a Git checkout,
validates archive members and paths, verifies incoming identity, copies the
listed build inputs, removes obsolete listed inputs and verifies the installed
identity. It does not itself compile the source or replace retained binaries.

The GUI action is named **Update MTS on servers**. It distributes the manager,
campaign directories (`simulations.json`, `resources.json`, `metadata.json`) and
the verified MTS2D source snapshot to their configured server locations. Reuse
the existing source-upload/verification machinery. Verify successful transfers
and report per-server failures. Updating source is distinct from compiling a new
working executable; retained executables and prepared run configurations remain
available to queued jobs.

Administrative records keep track of manager installation locations for later
SSH calls. Direct binary jobs do not use that installation, so changing its
location or Python package version cannot affect their startup.

## 7. Preparing a run and its submission records

Implementation reference: [runner.py](MTSManager/MTS_Management/runner.py),
currently `prepare()`.

Preparation occurs on the execution host before scheduling:

1. Read original settings; resolve campaign descriptions where needed without
   changing configuration identity.
2. Retain/verify the selected executable and original build record. Permit an
   older build schema. Record its actual capabilities; do not assume old binaries
   understand newly introduced CLI options.
3. Resolve/create the output root. Under a preparation lock, read the run index
   and find or determine the hash-associated folder. Reject a truncated-hash
   collision or inconsistent existing manifest.
4. Check indexed original settings and selected executable against existing run
   records. Use another output root for a different executable comparison or an
   independent identical-settings repetition.
5. For a new folder, create `config.conf` and `manifest.json` before submission.
   For an existing folder, preserve the original config and verify its identity;
   do not replace it with current defaults or expanded metadata settings.
6. Record a fresh or resume invocation. For the target interface, this is a
   binary path plus run-directory path, optionally `--resume`.
7. Create the attempt directory and save its request, environment, resources
   and complete shell script/command. Record an explicit dependency for a chain.
8. Update the index, invalidate relevant inventory data and record preparation
   and submission outcome. A preparation record is not evidence that a process
   has started.

The manager may reject duplicate active submissions using scheduler/receipt
information, but MTS2D owns the definitive lock against concurrent execution.
The manager does not select a checkpoint, hash a future latest checkpoint or
maintain a mutable Python worker state machine during scientific execution.

Configuration files remain readable in their prepared folders. Their identity
is established at preparation, and normal tools preserve them. There is no
additional per-attempt configuration copy requirement in this design.

## 8. Launch modes and the queued command

The queued script records setup, environment, log destination and the retained
binary invocation. Schematic Slurm script:

```sh
#!/bin/bash
set -euo pipefail
# Site setup and required modules
export OMP_NUM_THREADS=16
export OMP_PLACES=cores
export OMP_PROC_BIND=close
exec /absolute/retained/MTS2D /absolute/output/run --resume
```

The number of threads and binding settings come from the actual configuration
and site. Submit `sbatch` with its output directed to the attempt log, explicit
resource requests, one task and the appropriate allocation. Validate supported
resource options and returned scheduler ID at submission. Persist that ID and
the exact submitted command.

Neither `python -m MTS_Management _execute` nor a manager installation directory
appears in the script. The scheduler's exit status describes the binary because
the script uses `exec`. Local foreground/detached modes use the equivalent direct
command and capture output without importing a queued Python worker.

For a chain, queue each allocation with the same folder-based `--resume` command
and an explicit predecessor dependency. Selection of the newest checkpoint is
deferred naturally until MTS2D acquires the folder lock and reads it. If a prior
allocation already completed the scientific run, the next invocation exits
successfully after recognizing completion.

The precise continuation policy after cancellation, numerical failure or
out-of-memory must be chosen explicitly when implementing dependencies. A
dependency alone must not silently introduce numerical retry semantics. The
existing implementation's scheduler-state allowlist is documented in the old
reference; this target does not require a Python accounting query inside jobs.

## 9. MTS2D startup checks for a prepared folder

MTS2D performs the following before modifying scientific output:

1. Resolve the supplied run-directory path and require a readable `config.conf`.
2. Acquire a nonblocking run-directory lock and hold it until process exit.
   A second process targeting the same folder fails explicitly, regardless of
   whether either was launched by the manager.
3. Parse configuration using the executable's supported settings and validate
   protocol, backend availability and numerical parameter constraints.
4. Verify that resolved output belongs to the supplied folder. Folder-based
   operation does not search other storage devices or ask an unattended worker
   to approve a different output location.
5. Check scientific completion using the termination summary and applicable
   load/target state. Recognize sampling-target completion below the load limit.
6. With `--resume`, discover checkpoints inside this folder and load the selected
   state. Without it, reject existing scientific output requiring continuation;
   do not silently clear an incomplete run.
7. If resuming a prepared folder with no checkpoint and no previous scientific
   output, initialize a fresh state. If previous progress exists but no usable
   checkpoint exists, raise an error rather than restart from zero.
8. Validate mesh dimensions and boundary topology on checkpoint loading, restore
   state, and recover output consistently with the checkpoint.
9. Record resolved configuration/backend and selected checkpoint in simulator
   output without changing the identity-bearing `config.conf`.
10. Set up RNG, OpenMP, solver arrays and physical output, then run the protocol.

The simulator need not import campaign files, Python manager code or current
manager defaults. Its original configuration and retained executable determine
behaviour. Supplemental campaign metadata is used by administrative/read tools
to interpret historical settings, not as a runtime requirement for queued jobs.

## 10. Command-line interface and backward compatibility

Implementation references: [main.cpp](MTS2D/src/main.cpp),
[param_parser.cpp](MTS2D/src/Data/param_parser.cpp),
[param_parser.h](MTS2D/src/Data/param_parser.h).

The new folder argument identifies configuration and output in one location.
`--resume` requests simulator-owned continuation; it is not a manager property.
Retain explicit `-c`, `-d` and `-o` paths for direct scientific replay and existing
uses where applicable. An explicit checkpoint selects that file; it must not be
silently replaced by an automatic latest selection.

Invalid arguments, unreadable configuration, incompatible state and startup
failure return a nonzero exit. Already-complete detection returns zero with an
explicit message. A fresh invocation must not clean existing scientific data
merely because its load is below the final budget. Destructive rerun behaviour
is separate from ordinary fresh/resume startup.

### 10.1 Backend resolution

New manager-generated configurations explicitly record `tangentBackend`, with
CHOLMOD the current new-run default. A checkpoint's saved selection is preserved
when its accompanying configuration omits the setting. A historical checkpoint
without the field selects Eigen. An explicit supported configuration value takes
precedence. An unavailable requested backend raises an error.

Do not append resolved missing settings to old `config.conf` files. For older
campaigns, metadata supplies the manager/readers with the known historical value
without pretending that the retained binary supports current defaults or schema.

### 10.2 Existing retained executables and queued scripts

An old retained executable cannot gain `--resume` or folder-based discovery by
updating manager code or syncing current simulator source. Existing queued
scripts continue to run their recorded entry point. Keep their inputs and
required historical manager installation available until those jobs finish.

Use the new folder command only with binaries that implement it. Historical
binary launches can retain their supported explicit configuration/checkpoint
interface. They are not automatically converted into current-interface jobs,
and no old folder is silently switched to a newly built binary. The rollout
must preserve the existing benchmark's Eigen executable and original identities.

## 11. Fresh initialization and prepared output

Implementation references: [simulation.cpp](MTS2D/src/Simulation/simulation.cpp),
[data_export.cpp](MTS2D/src/Data/data_export.cpp).

The folder and configuration already exist before queueing. Directory existence
therefore does not mean a simulation has started. MTS2D distinguishes a prepared
folder containing administrative records from one containing scientific output.

Fresh initialization retains mesh construction, seeded disorder/noise, boundary
constraints, driven-node groups and solver setup. Relaxation/reversibility retain
the existing standardized single-thread LBFGS initial relaxation, temporary
preconditioner/tolerance choices and restoration of the requested step settings.
`noRelaxation` records starting geometry without initial relaxation/noise.

Reuse the current protocol and numerical initialization routines. Split their
scientific initialization from the old constructor's cleanup behaviour so a
folder-based invocation does not erase progress. In particular, a stale summary
must not be mistaken for completion of a different state, and a completed
sampling run must not be treated as an incomplete fresh run solely because its
load is below the strain limit.

MTS2D owns the lock and checks output state before opening files for append or
cleanup. No manager worker is required to protect this path.

## 12. Simulator-owned resume and output recovery

Implementation references: `Simulation::loadSimulation()` in
[simulation_outputs.cpp](MTS2D/src/Simulation/simulation_outputs.cpp),
[data_csv.cpp](MTS2D/src/Data/data_csv.cpp),
[cereal_help.h](MTS2D/src/Data/cereal_help.h).

`--resume` performs checkpoint discovery after acquiring the run lock. It must
select a completed, restorable checkpoint representing resumable protocol state.
Checkpoint files can include periodic slots, load-named dumps and crash/replay
diagnostics; the latter must not accidentally become the normal continuation
state merely because their filesystem modification time is newest.

The exact ranking and treatment of incomplete/corrupt checkpoints remain an
implementation decision to document and test. Do not silently adopt the old
manager's greatest-modification-time rule as a scientific definition of latest.
Record the selected checkpoint and its restored load/step. Prefer a shared
implementation for automatic and explicit loading, with errors for ambiguous or
unexpected data. Any fallback to an earlier checkpoint must be explicit in the
output rather than an unreported restart.

Checkpoint publication must allow discovery to distinguish completed files from
an interrupted write. Temporary serialization/compression files are not resume
candidates. Open/compression failures must surface as errors rather than a
success message for a missing or partial checkpoint.

After selection, retain the scientific recovery checks:

1. Deserialize the physical state and resolved backend.
2. Require matching dimensions and periodic/nonperiodic topology.
3. Keep constraints/reference geometry from the saved state.
4. Reevaluate load/target completion and exit successfully if already complete.
5. Recover supported macro-output columns; require matching CSV schema.
6. Back up according to the existing output policy, trim rows beyond the loaded
   `load_step`, and append from the restored state. Diagnostic removal of a
   failed current-step row remains a separate numerical-recovery operation.
7. Reject malformed load-step values, rebuild solver arrays, and refresh mesh
   geometry, forces and derived quantities.

MTS2D supplies these steps to direct and scheduled invocations alike. The manager
neither chooses a checkpoint nor trims CSV output. A resume failure is visible
through nonzero simulator exit, its log and scheduler accounting.

## 13. Loading, minimization and scientific stopping

Implementation references: [protocols.cpp](MTS2D/src/Simulation/protocols.cpp),
[simulation_minimization.cpp](MTS2D/src/Simulation/simulation_minimization.cpp),
[step_termination.cpp](MTS2D/src/Simulation/step_termination.cpp).

The protocol validates settings again. Resumed states preserve their boundary
constraints and reference geometry rather than preparing them a second time.
Termination counters are part of the saved simulation and are reinitialized
against current targets. Initialization requires finite positive reference
element areas and total reference volume. Plastic/elastic classification also
requires a finite positive reference shear modulus.

The loading loop continues while `mesh.load < startLoad + totalLoad` and enabled
sampling targets have not been met. Each iteration:

1. Derives the direction from saved path load, segment length and repeat mode.
2. Shortens the next increment at a segment boundary or the end of the budget.
   Requires a positive step that advances floating-point load.
3. If enabled by `checkpointInterval`, writes alternating `checkpoint_0` and
   `checkpoint_1` states before the next deformation. This calls
   `saveSimulation()` directly, independently of `writeDumps`.
4. Ensures forces and records the pre-step energy.
5. Applies the selected mode: deform/noise/minimize for relaxation; forward/reverse
   checking for reversibility; deformation and reconnection without minimization
   for `noRelaxation`.
6. Sets the accepted path endpoint and updates termination counters.
7. Records physical output and progress through `finishStep()`.

The path coordinate increases even for negative physical deformation. Starting
deformation consumes no additional loading budget. Solver state is rebuilt on
initialization; LBFGS correction count is capped at problem dimension, with
invalid reset dimensions/counts rejected.

Preconditioner selection is checked in MTS2D. Rebuild occurs when absent or
incompatible, on `every`, or after the adaptive displacement threshold. A failed
preconditioner build increments fallback statistics and continues the solve
without that preconditioner; this is an explicit numerical fallback, not a
manager retry. The assembled factor is held fixed for that minimization.

The termination recorder requires finite pre-step, final and initial-guess
energies and a finite positive increment. It counts energy increases/drops using
reference-volume-normalized energy change and `stepEnergyTolerance`. When
plastic/elastic sampling is enabled, excessive energy increase during relaxation
and an invalid classification threshold raise errors. Enabled count targets
combine according to `stopWhen=all` or `any`. The stop reason becomes
`sample_targets` or `strain_limit`.

These are lifecycle-level numerical checks. Element, force, geometry and solver
implementations contain further local mathematical invariants; the manager does
not repeat those calculations or validate their results independently.

### 13.1 Numerical failure and replay

`Simulation::applyDeformation()` saves an in-memory state before a loading
deformation. Minimizer exceptions are formatted with context and attempt forced diagnostic
output. An eligible LBFGS failure can restore the state and retry with one
additional correction. Successful retry removes the failed CSV row and restores
the configured correction count for subsequent steps.

If that retry is unavailable or fails, the simulator can restore the pre-step
state, save a crash dump and replay with minimization logging enabled. It reports
an error whether the replay reproduces the failure or unexpectedly succeeds.
This diagnostic replay is not automatic submission of another scheduler job.

## 14. Progress, periodic output and scientific completion

Implementation reference: [simulation_outputs.cpp](MTS2D/src/Simulation/simulation_outputs.cpp).

`finishStep()` refreshes geometry, force-state averages, plastic-event measures,
centre of mass, bounding box, energy history and participation measures. It
updates progress, writes the macro CSV and configured mesh/minimization output,
calls an optional step logger, resets counters and releases the in-memory replay
checkpoint.

Console progress is updated on integer progress changes or a 20-second interval
when not quiet, flushing the macro CSV. Progress crossing a suitable 5% boundary
can request an additional dump. Ordinary `m_writeDump()` requires `writeDumps`;
its triggers include midpoint, elapsed hour, final load, an explicit target load
or forced output. Duplicate default dumps for the same load step are suppressed.
VTU and minimization diagnostics have their own output settings and retention
conditions and do not run solely because the manager requests a checkpoint.

`finishSimulation()`:

1. Serializes termination counters, reference quantities, classification,
   `targetsMet` and `stopReason` to `termination.json.tmp`.
2. Checks stream errors and atomically renames it to `termination.json`.
3. Gathers mesh files into the collection output.
4. Attempts a forced ordinary dump, still subject to `writeDumps`.
5. Flushes macro and minimization CSV streams.

The summary is published before the remaining final output operations. Its
presence therefore does not prove that every subsequent output operation or
the complete process succeeded.

The target entry point returns zero for normal scientific termination or an
already-complete run, and nonzero for argument, configuration, checkpoint or
simulation failure. Scheduler success alone still does not establish a specific
scientific stop reason; read the termination summary and scientific output.

## 15. Logs, history, process exit and cancellation

Scientific history is written by MTS2D: macro data, checkpoints, diagnostic
output and termination summaries. A scheduler captures stdout/stderr for each
attempt and records scheduler ID, allocation state, exit code and termination
reason. Submission records associate those scheduler/log records with the run,
retained binary and exact command.

The manager does not need a worker that streams every line through Python,
duplicates simulator completion in `status.json`, or appends a Python lifecycle
event for every runtime transition. Its journal can retain preparation,
submission, transfer and administrative events. Runtime state is read from the
scheduler/process and simulator outputs. Logs remain separate per attempt.

For local detached execution, preserve the launched PID/process-start identity
and capture output. The mechanism for obtaining a durable local exit code after
the initiating process closes must be selected during implementation; do not
introduce a dependency on the changing manager package to solve it. Local and
Slurm monitoring must distinguish unknown process outcome from scientific
completion.

Stop requests use `scancel` for Slurm or a verified local process identity/group.
MTS2D releases its run lock when the process exits, including OS cleanup after
termination. The target does not assume that termination signals always allow
a final checkpoint or summary to be written. Resume uses already completed
checkpoints. Adding signal-driven saving is a separate simulator capability,
not a guarantee supplied by scheduler cancellation.

Historical Python-worker jobs can still have request/status/journal records in
the old format. Readers preserve that history and distinguish it from new direct
jobs; updating an inventory does not manufacture missing runtime events.

## 16. Campaign submission and benchmark-specific checks

Implementation reference:
[launch.py](MTSManager/campaigns/magi_benchmark_oct26/launch.py).

Campaign readers and benchmark launchers use the common campaign directory:
`simulations.json`, `resources.json`, and optional supplemental `metadata.json`.
The benchmark retains plan checks for expected run counts, diagnostic labels,
size-dependent resource requests, portable build settings and recorded source
revision. Definition checksums cover simulation definitions, not mutable metadata.

The controller retains its submission lock and explicit resume of submission
receipts. It verifies existing run settings/binary association, accepted scheduler
IDs and receipt consistency, and refuses to resubmit an uncertain job blindly.
Receipt continuation is distinct from simulator `--resume`.

Prepare each required run folder and its config before submitting that run.
Write/fsync confirmed receipts and report partial submission failures without
deleting already accepted jobs. A campaign is not an atomic scheduler transaction.
The binary is retained per job preparation, with existing copies reused; there
is no additional campaign-level binary pinning mechanism.

For future allocations of the same target-interface run, queue its direct
folder-based resume command. Do not depend on shared job names to establish
ordering. Metadata updates do not alter the queued command or old config hash.

## 17. Inventory, GUI monitoring and retrieval

Implementation references: [inventory.py](MTSManager/MTS_Management/inventory.py),
[inventory_cache.py](MTSManager/MTS_Management/inventory_cache.py),
[status.py](MTSManager/MTS_Management/status.py),
[server.py](MTS_GUI/server.py), `cluster.fetch_many()`.

### 17.1 Inventory

Retain indexed discovery, bounded CSV-tail reads, cached finished data,
concurrency-safe cache publication and optional bounded storage estimates.
Validate original configuration/folder/manifest identity before interpreting
settings using current campaign metadata.

New direct jobs derive allocation state from scheduler information or verified
local process identity. Scientific completion and progress derive from MTS2D
output. Prepared/submitted records determine which attempt/log belongs to a run;
absence of a Python-written `status.json` is not an error for a direct job.
Display unknown or conflicting evidence instead of assuming success when a job
disappears from the queue. Preserve reading of historical attempt records.

Sampling-target termination is complete even if its load fraction is below 100%.
A terminated allocation can leave a resumable but scientifically incomplete run.
Do not collapse those two states. Metadata changes invalidate derived setting
interpretations in caches without changing run identities or scientific data.

### 17.2 GUI monitoring and help

Selecting a campaign reads its `simulations.json`, metadata, cached server
inventories and local copies. Renamed readers discover `simulations.json`
consistently. Refresh updates selected server records; failures preserve prior
cached results. Hard inventory refresh rereads scientific files without removing
them. Source coverage and missing campaign members remain visible.

Start simulations and Monitor data explain configuration identity, prepared
folders, retained binaries, direct execution, simulator-owned resume/locking,
metadata interpretation, scheduler state and scientific completion. Their help
must describe this architecture only once it is implemented.

The advanced update action is **Update MTS on servers**, distributing the
manager, campaign files and simulator source. Queued direct jobs continue using
their retained binaries and prepared configs after that operation.

### 17.3 Retrieval

Retain unambiguous hash selection, manifest/configuration validation, safe
destination checks, grouped transfer and retrieval receipts. Read original
identity before supplemental settings. Make the corresponding campaign files
available locally when retrieving historical runs, so offline interpretation
can resolve missing settings without network access.

Macro retrieval retains macro/configuration/manifest/termination records; full
retrieval additionally retains the run tree and logs. A data download does not
implicitly copy a runnable binary or turn it into another scheduled job.

## 18. Target responsibility table

| Procedure/check | Manager or scheduler | MTS2D |
| --- | --- | --- |
| Create complete new settings | Manager defaults and submission validation | Validate actual file when invoked directly or scheduled |
| Preserve historical identity | Original settings/hash; editable campaign descriptions for absent keys | Preserve original config; record resolved behaviour in output |
| Prepare output folder/config | Before submission | Read supplied folder; distinguish prepared from previously executed |
| Retain/select binary | Verify original build/bytes; share copies across jobs | Execute selected binary; no Python version negotiation |
| Build current source | Build-time schema agreement and verified source | Compiled implementation |
| Use older binary | Permit different build-time schema; use its supported CLI | Historical behaviour remains that of the retained binary |
| Queue resources/dependencies | Explicit scheduler requests and predecessor IDs | No scheduler control |
| Start a queued job | Saved shell command directly invokes binary | Own startup and physical execution |
| Select/load latest checkpoint | No manager selection | `--resume`: discover completed protocol state, load and recover output |
| Prevent concurrent writes | Serialize preparation; optional submission duplicate checks | Definitive run-folder process lock |
| Recover CSV and numerical state | Read-only inspection | Check dimensions/topology, trim/recover output and rebuild solver state |
| Numerical recovery | No wrapper retry policy | Existing numerical fallback, LBFGS retry and diagnostic replay |
| Record scientific completion | Read and display it | Macro data, termination summary, logs and checkpoints |
| Record submission/allocation | Submission receipts plus scheduler/process records | Log actual resumed state and scientific outcome |
| Cancel execution | Scheduler/local process signal | Release lock; resume from completed checkpoints on later invocation |
| Distribute updates | Manager, three campaign files and MTS2D source | Retained binaries unchanged by source sync |

Implementation decisions still requiring resolution are checkpoint ranking and
corrupt-file treatment, chain dependency policy after unsuccessful allocations,
durable local exit reporting and coexistence with historical queued workers.
These do not change the ownership rules above. They must be documented precisely
as part of implementing their respective paths.

## 19. Implementation map and verification requirements

| Area | Existing implementation to review |
| --- | --- |
| Campaign filenames/metadata | `campaign.py`; GUI campaign discovery; campaign launchers, examples and retrieval |
| Original versus effective settings | `Config.read`, `config_hash`, `as_dict`; manifests/index validation and readers |
| Build schema versus retained binary | `verify_schema`, `executable_identity`, `retain_executable` |
| Prepare folders before queueing | `runner.prepare`; GUI/CLI and remote `serve` submission paths |
| Direct invocation and captured logs | `runner.launch`, `_execute` callers, Slurm script generation and detached launch |
| Folder CLI, resume and lock | `main.cpp`; `Simulation` construction; checkpoint loading and completion detection |
| Preserve config and publish checkpoints | `saveConfigFile`; `saveToFile`; dump discovery/publication |
| Runtime/read-side state | `inventory.py`, `status.py`, GUI monitoring and local process records |
| Server update | `deployments.update_managers`, source archive/install functions and GUI action labels |

Focused verification must establish that:

1. Existing campaign/run hashes and retained Eigen binary associations survive
   filename changes and editable supplemental metadata.
2. An older build-schema record is not rejected solely because a manager setting
   has been introduced; binary checksum mismatches still fail preparation.
3. Prepared folders/configs exist before submission and direct scripts do not
   import the installed manager at runtime.
4. A target-interface job still starts after manager code is updated or moved,
   provided its binary, configuration and required runtime environment remain.
5. Simulator `--resume` handles a pristine prepared folder, interrupted progress,
   already-complete load and sampling runs, and explicit checkpoint loading.
6. The simulator prevents two processes from writing the same run concurrently.
7. Checkpoint selection/publication and CSV recovery preserve scientific state;
   retain and extend the simulator's existing numerical tests as needed.
8. Inventory and retrieval work for historical wrapper jobs and new direct jobs,
   including local data interpretation with supplemental campaign metadata.

Keep this target description and the archived implementation reference distinct.
When implementation changes land, update the design status and source map to
identify which interfaces are available. Retaining an old executable does not
retroactively add new command-line or checkpoint behaviour to it.
