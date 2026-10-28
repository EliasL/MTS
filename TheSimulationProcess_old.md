# The simulation process

This document describes the implemented path from configuring a simulation or
campaign to submission, execution, continuation, termination and retrieval. 

## 1. Components and terminology

| Component or record | Responsibility |
| --- | --- |
| MTS GUI | Creates configurations, invokes manager operations and displays cached inventories and local copies. |
| MTSManager client | Reads configurations and site profiles; submits local or remote requests; records submission outcomes. |
| MTSManager on the execution host | Prepares run directories, retains executables, submits jobs and runs the execution wrapper. |
| Slurm | Queues allocations, applies resource limits and dependencies, starts submission scripts and records scheduler state. |
| MTS2D | Parses simulation settings, initializes or loads physical state, performs loading/minimization, writes scientific output and checkpoints, and evaluates scientific stopping conditions. |
| Configuration | The simulator settings represented by a manager `Config`, excluding its generated name. |
| Run | An output folder associated with one configuration hash and one executable checksum. |
| Attempt | One prepared execution of a run, identified by a UUID and stored below `attempts/`. |
| Campaign | A named list of labelled configurations. The name groups records; it does not select or pin an executable. |
| Retained build | A saved executable with its original build manifest, shared by any number of attempts. |
| Checkpoint or dump | A serialized MTS2D simulation, normally an XML document compressed as `.xml.gz`. |

There are three different meanings of completion:

1. **Submission complete:** Slurm or a detached launcher has accepted the job.
2. **Attempt finished:** the manager observed simulator exit code zero.
3. **Scientific termination:** MTS2D reached its load budget or enabled sampling
   targets and wrote `termination.json`.

These meanings are not interchangeable. A submission receipt does not establish
execution success. A zero exit code can also mean MTS2D detected an already
complete simulation.

## 2. Files and identities

The following layout is schematic. Site profiles determine absolute locations.

```text
MTSManager/
  sites.toml
  campaigns/<campaign-directory>/
    campaign.json
    campaign.resources.json          # Used by the MAGI benchmark launcher
  .state/
    job-log.jsonl
    journal-cursors.json
    manager-deployments.json

execution-workspace/
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
  .inventory.lock
  .inventory.sqlite3
  <label>[<first-20-configuration-hash-digits>]/
    config.conf
    manifest.json
    .run.lock
    macroData.csv
    termination.json                 # Published at scientific termination
    data/                            # Mesh output, when enabled
    dumps/
    backups/
    attempts/<attempt-id>/
      request.json
      status.json
      .status.lock
      submit.sh                      # Slurm mode
      launcher.log                   # Slurm or detached wrapper output
      stdout.log                     # Simulator stdout and stderr
```

The executable archive used by `prepare()` is `state_dir.parent/executables`.
The build operation can use a separately configured archive location. Already
retained binaries are reused without another copy.

### 2.1 Configuration identity

`Config.config_hash` is SHA-256 of canonical JSON containing:

```json
{"format": "mtscripts-config-v1", "settings": {"...": "all settings"}}
```

Canonical serialization sorts keys, uses compact separators and rejects NaN.
The historical format string is intentional and independent of the package
name. Every setting in `Config.as_dict()` participates, including output,
thread-count and backend settings. Labels, campaign names and site paths do
not participate. Adding a setting changes the hash even if its value describes
the behaviour of an older executable.

Folder names contain the first 20 hexadecimal digits. Manifests, requests and
indexes retain the full digest. Labels must be 1–120 ASCII characters, beginning
with a letter or digit, and otherwise use letters, digits, `_`, `.` or `-`.

### 2.2 Other identities

| Identity | Construction and use |
| --- | --- |
| Executable checksum | SHA-256 of the binary bytes. Included in the run manifest and attempt identity. |
| Build-record hash | Digest of the complete build manifest. Names a retained build directory; two records with identical binary bytes can still have different record hashes. |
| Source identity | Git revision, main-branch provenance and a digest of tracked build inputs, or the corresponding uploaded snapshot record. |
| Configuration-schema checksum | Digest of the sorted current manager setting names. Recorded in build manifests and currently enforced when selecting or executing a binary. |
| Manager identity | Version plus digest of top-level `.py` and `.json` files in `MTS_Management`. Recorded separately at submission and execution. |
| Checkpoint checksum | SHA-256 of an explicitly selected checkpoint. A latest-checkpoint request obtains this when the worker starts. |
| Attempt `run_identity` | Digest of configuration hash, executable checksum and checkpoint checksum, or null for a fresh launch. |
| Manifest checksum | Digest of the manifest file bytes. Used to compare inventories and downloaded copies. |

Retaining a binary does not retain Python, shared libraries, modules or the
operating system. The submission script and environment records describe part
of that execution context.

## 3. Configuration creation and validation

Sources: [config.py](MTSManager/MTS_Management/config.py),
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

### 3.2 Saved configuration files

`Config.read(path, expected_name=...)` is stricter than new configuration
creation. It removes comments and blank lines, reads `key = value` pairs,
rejects duplicate keys, extracts `name`, and requires exactly the current
setting-name set. It does not fill missing saved settings from defaults.
Booleans must be `true` or `false`; remaining values use default-derived types.
After normal validation, it verifies the name/hash combination and, where
provided, the expected output-folder name.

Writing a configuration places `name` first, followed by every setting in sorted
order. There is currently one shared `config.conf` per run, not a separate
configuration snapshot for every attempt.

### 3.3 Campaign files

`campaign_entries()` requires exactly the top-level keys `name` and `runs`, with
a nonempty run list. Each entry must have exactly `label` and `settings`.
It constructs a validated `Config`, checks that the supplied settings were
complete, validates the label, and rejects duplicate configuration hashes.
Two entries differing only by label are therefore duplicates.

`campaign_identity()` is a separate reader for monitoring. It validates the
outer structure, label and settings-object type, then hashes the saved settings
directly. It does not fill current defaults or perform the full `Config`
validation. This allows identification of a saved plan independently of its
current executability. Normal `json.loads()` parses the complete campaign file.

Neither reader currently accepts a top-level `metadata` field or resolves
missing settings from campaign metadata.

`make-campaign` constructs a Cartesian product of requested axes, requires
nonempty list axes, and refuses an existing destination. The convenience axis
`L` sets both dimensions and cannot be combined with `rows` or `cols` axes.

## 4. GUI and CLI entry points

Sources: [app.js](MTS_GUI/static/app.js),
[server.py](MTS_GUI/server.py), [cli.py](MTSManager/MTS_Management/cli.py).

### 4.1 GUI submission

1. The browser collects dimensions, seed, threads, boundary, load budget,
   increment, reconnection, label and site.
2. Its initial settings also select zero initial noise and disable mesh VTUs,
   ordinary dumps and VTU frames. Advanced JSON overrides these values and the
   visible form values.
3. The server creates a `Config`, applying current defaults and validation.
4. It writes a configuration under the GUI state directory's `configurations/`,
   named with the full configuration hash.
5. It invokes `python -m MTS_Management --sites ... run <configuration>
   --site <site> --campaign GUI`, with an optional label.
6. For the local site it explicitly requests detached execution.
7. The GUI reports submission, not scientific completion. Closing the GUI does
   not terminate an accepted detached or Slurm job.

The GUI permits one background operation at a time. Its operation log and task
state are separate from the manager job journal and simulator output.

### 4.2 CLI dispatch

`run` reads one saved configuration. `campaign` calls `campaign_entries()`.
The CLI loads site profiles and chooses a local journal directory. A requested
chain count must be positive; multiple allocations require Slurm.

For a local foreground campaign, it prepares all attempts before launching
them. If preparation fails, already prepared attempts are marked stopped. One
attempt is launched directly; multiple attempts use `run_many()` with a positive
worker count and separate Python wrapper processes. Failure or interruption
terminates outstanding wrapper processes and stops work not yet started.

Other modes submit entries sequentially through `cluster.submit()`. There is no
transaction spanning an entire campaign: earlier accepted jobs remain submitted
if a later submission fails.

## 5. Site selection and remote transport

Source: [cluster.py](MTSManager/MTS_Management/cluster.py).

`load_sites()` requires a TOML document containing only `sites`. Profiles define
working/source/executable/output/state paths, Python, manager installation,
execution mode, setup commands, environment and Slurm options.

Profile validation requires a valid Python module name, a constrained SSH alias,
one of `foreground`, `detached` or `slurm`, Boolean enable/native-architecture
flags, a positive integer CPU multiplier, and valid environment variable names
with string values. Disabled connections are excluded by default.

Local working directories are resolved relative to `sites.toml`. Remote path
resolution occurs on the execution host. The manager deployment registry can
override a profile's manager installation without changing its simulator,
output or state paths.

`cluster.submit()`:

1. Appends `submission_requested` to the client journal.
2. Calls the execution host with complete settings, campaign, label, checkpoint
   and chain count.
3. On transport/server failure, records `submission_error` with an explicitly
   unknown remote outcome. It does not automatically resubmit.
4. On success, records `submission_accepted` and imports remote journal events.

`cluster.call()` rejects disabled sites. A local site without setup runs
`serve()` directly. Other calls launch a Python `_serve` process through a local
shell or SSH, pass canonical JSON on stdin and expect a JSON response on stdout.
Setup commands run first. A shared manager directory is added to `PYTHONPATH`;
the remote Python command uses `-P` to avoid an older package in the working
directory shadowing it. Reader operations also set a remote alarm. Client calls
have bounded timeouts and terminate their transport subprocess on cancellation
or timeout. A transport timeout does not prove that submission was rejected.

On the execution host, `serve(operation="run")` requires the exact current
setting-name set and reconstructs a validated `Config`. Remote foreground mode
is converted to detached mode so the job survives SSH disconnection.

Slurm CPU count is initially `nrThreads * slurm_cpus_per_thread`, then merged
with explicit site Slurm options. Multiple-allocation chains require Slurm and
`writeDumps=true`; later allocations request `checkpoint="latest"` and identify
their preceding attempt.

## 6. Source installation, builds and retained executables

Sources: [provenance.py](MTSManager/MTS_Management/provenance.py),
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

`executable_identity()` performs the binary/main-branch checks and additionally
requires the build's setting-name checksum to equal the current manager schema.
This check currently rejects older retained binaries when a setting is added,
even if their binary bytes are intact. It occurs during preparation and again
when the queued wrapper starts.

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

The GUI's current **Update all server managers** action instead calls
`update_managers()`. It groups profiles by SSH destination, requires one shared
manager directory per destination, and synchronizes the `MTS_Management`
package's Python/JSON files there. It deletes obsolete package files, verifies
the deployed manager identity and records successful profile activation.
Failures are reported per profile; a completely unsuccessful update raises an
error. This action currently uploads neither campaigns nor MTS2D source and
does not rebuild binaries.

Existing Slurm scripts keep their recorded working directory and Python command.
Updating a different manager installation does not redirect those scripts.

## 7. Run and attempt preparation

Source: `runner.prepare()` in [runner.py](MTSManager/MTS_Management/runner.py).

Preparation runs on the execution host, before `sbatch`:

1. Retain and verify the selected executable, including the current schema check.
2. Resolve/create the output root. Interpret `checkpoint="latest"` as deferred
   selection; resolve an explicit checkpoint path immediately.
3. Require dumps or periodic checkpoints for a latest-checkpoint request. Require
   an explicit checkpoint to exist as a file.
4. Acquire output-root `.prepare.lock` and read `.run-index.json` if present.
5. Compute the desired folder name. Reuse an indexed folder for the full
   configuration identity, and reject a conflicting hash with the same 20-digit
   prefix. Labels do not create an independent run for identical settings.
6. For a chain continuation, require latest-checkpoint mode, a predecessor below
   this run's `attempts/`, and the most recently created attempt with a scheduler ID.
7. For an existing managed run, require manifest settings, configuration hash and
   executable checksum to match. Reject any active attempt unless preparing the
   allowed chain continuation. Active means `prepared`, `queued`, `running` or
   `stopping`.
8. Reject a fresh launch of a previously started run or a run with macro output;
   require an explicit continuation request instead. Re-read its saved
   configuration and check its folder/name/hash.
9. For a new run, reject an existing unmanaged directory and reject checkpoint
   continuation without the original managed folder. Create the run folder,
   complete `config.conf` and `manifest.json`.
10. Require an explicit checkpoint's resolved path to lie within the run folder.
11. Invalidate that run's cached inventory before preparing another attempt.
12. Create a UUID attempt directory. Compute an explicit checkpoint checksum if
    present; create `run_identity` and `request.json`.
13. Record campaign, site, host, absolute paths, retained executable/build,
    submission manager identity, input checkpoint, deferred-latest flag,
    predecessor, environment and creation time in the request.
14. Atomically write `status.json` as `prepared`, update the run index and append
    the journal event `requested`.

Most JSON record writes use a same-directory temporary file, flush/fsync and
atomic replacement. The multi-file preparation sequence is not one atomic
transaction; an exception can leave files requiring inspection.

## 8. Launch modes and the exact queued command

Source: `runner.launch()`.

### 8.1 Foreground and detached execution

Foreground mode calls `execute(attempt)` in the current process. Detached mode
starts a new session containing:

```sh
/recorded/python -u -m MTS_Management _execute /absolute/run/attempts/UUID
```

Its working directory is `PACKAGE_ROOT`, its stdin is disconnected, and wrapper
stdout/stderr append to `launcher.log`. The launcher PID and process-start token
are saved. A local reaper thread can collect the launcher without controlling
its lifetime.

### 8.2 Slurm submission

Only `time`, `partition`, `account`, `qos`, `mem` and `cpus-per-task` options are
accepted by `launch()`. It rereads the saved configuration and rejects an
allocation with fewer logical CPUs than configured threads.

It writes `submit.sh` with this structure:

```sh
#!/bin/bash
set -euo pipefail
# Site-specific setup commands, for example module loading
cd /absolute/manager/package-root
exec /recorded/python -u -m MTS_Management _execute /absolute/run/attempts/UUID
```

It calls `sbatch --parsable` with the run folder name as job name, output directed
to `launcher.log`, one node, one task and the selected resources. A chain adds
`--dependency=afterany:<predecessor-job-id>`. Matching job names alone do not
serialize these submissions; the dependency is on an explicit scheduler ID.

Before calling `sbatch`, it records `queued` with resources and predecessor to
handle a worker starting immediately. After submission, it requires a numeric
job ID, saves it without overwriting a worker's newer state, and appends
`submitted`. A launch exception marks a still-prepared/queued attempt failed.

The queued command is therefore a **manager wrapper**, not the simulator binary.
Slurm retains the submitted script, but that script imports Python code from a
filesystem installation when it executes.

## 9. Checks performed when the wrapper starts

Source: `runner.execute()`.

The wrapper reads the saved request and tries a nonblocking run-wide `.run.lock`.
It accepts only a `prepared` or `queued` attempt, checks that state again inside
its execution handler, and then:

1. Reads `config.conf` using the executing manager's current strict schema and
   checks the folder name and request configuration hash.
2. Verifies binary bytes, recorded main-branch provenance and current schema
   compatibility; requires the complete build record to match the request.
3. Computes the execution manager identity. It records this separately from the
   submission identity; a difference is not itself rejected.
4. For a chained predecessor, queries `sacct -X` and requires `COMPLETED`,
   `TIMEOUT`, `PREEMPTED` or `NODE_FAIL`. Other states, including cancellation,
   failure and out-of-memory, are rejected even though `afterany` released the job.
   A predecessor still marked active is recorded stopped with its terminal
   scheduler state.
5. For deferred latest selection, lists `dumps/*.xml.gz` and chooses greatest
   modification time. It does not rank by load encoded in filenames, exclude
   crash dumps, test several candidates or fall back after corruption. It errors
   if no candidate exists or its resolved path leaves the run folder.
6. Records that selected path and checksum, updates `run_identity` in the request,
   and appends `checkpoint_selected`.
7. Forms the simulator command using the retained executable, shared run
   `config.conf`, output root, and optional `-d <selected-checkpoint>`.
8. Rechecks any selected checkpoint's checksum immediately before launch.
9. Inherits the worker environment, overlays the saved site environment and sets
   `OMP_NUM_THREADS` from the configuration. On Linux it records CPU affinity and
   physical-core topology. With `OMP_PLACES=cores`, it rejects fewer allocated
   physical cores than configured threads.
10. Installs SIGTERM/SIGINT handlers when running on Python's main thread.
11. Under `.status.lock`, rechecks cancellation and starts MTS2D in a new process
    session, with the run folder as working directory and combined stdout/stderr
    captured through a pipe.
12. Records `running`, simulator/runner PIDs and process-start tokens, exact
    command, timestamp, execution manager identity, CPU allocation and
    OMP/GOMP/KMP environment variables. Appends the corresponding journal event.

Early failures acquiring the run lock or rejecting the initial state occur
outside the main execution exception handler and do not necessarily produce a
new failed status record.

No campaign file is consulted in this execution path. The request and run folder
already contain the selected execution inputs.

## 10. MTS2D command-line handling and configuration parsing

Sources: [main.cpp](MTS2D/src/main.cpp),
[param_parser.cpp](MTS2D/src/Data/param_parser.cpp),
[param_parser.h](MTS2D/src/Data/param_parser.h).

The wrapper's final command is equivalent to:

```sh
/retained/build/MTS2D -c /output/run/config.conf -o /output/root
# A continuation adds: -d /output/run/dumps/selected.xml.gz
```

MTS2D supports `-c` for configuration, `-d` for an explicit dump, `-o` for output,
`-r` to force rerun and `--makeDumpAt <load>`. The manager does not pass `-r` or
`--makeDumpAt` in its standard execution path.

With `-d`, MTS2D loads that specific checkpoint. If `-c` is absent, it attempts
to find `config.conf` from the dump's location and returns after printing an
error if no configuration is found. Without `-d`, it parses a configuration
and enters the new-simulation protocol. **The current MTS2D entry point does not
search for a latest checkpoint.** It does not compare a supplied dump with other
dumps to ensure that it is newest.

The C++ parser removes comments and whitespace, reads settings into a map, and
lets a later duplicate replace an earlier value. This differs from the manager,
which rejects duplicates. Missing settings receive simulator defaults; unknown
keys are rejected. Numeric conversions must consume the whole value; booleans
accept `true`, `false`, `1` or `0`.

Protocol validation checks loading/deformation/boundary/direction choices,
bounded and advancing load budgets, segment/repeat conditions, noise constraints,
reversibility tolerance, stopping-target validity, checkpoint interval and
reconnection choice. Preconditioner validation checks backend availability,
preconditioner/update choice and positive displacement threshold. Selecting
CHOLMOD in an Eigen-only build raises an error.

### 10.1 Backend defaults and saved configurations

- Fresh configurations omitting `tangentBackend` use the executable's compiled
  default, normally CHOLMOD in the current build configuration.
- Checkpoints store their backend. Loading an older checkpoint without that
  trailing field explicitly selects Eigen.
- On checkpoint loading, the config parser receives the checkpoint backend as
  the default for an absent setting. An explicit file value takes precedence.
- `saveConfigFile()` currently appends the resolved backend when the saved
  configuration lacks it, including when source and destination are the same
  file. This mutates that file and can invalidate a hash based on its old setting
  set. The behaviour belongs to MTS2D, independently of manager validation.

Neither MTS2D nor the manager currently implements campaign supplemental metadata.

## 11. Fresh simulation startup and existing output

Sources: [simulation.cpp](MTS2D/src/Simulation/simulation.cpp),
[data_export.cpp](MTS2D/src/Data/data_export.cpp),
[data_csv.cpp](MTS2D/src/Data/data_csv.cpp).

The `Simulation` constructor loads settings, seeds the random generator, chooses
OpenMP threads, constructs the mesh, sets starting load and prepares CSV columns.
`m_loadConfig()` uses approximately 75% of OpenMP's reported maximum, at least
one thread, when `nrThreads=0` or when the request exceeds that maximum. The
manager rejects zero threads earlier, but direct MTS2D invocation can reach this
behaviour. Nested OpenMP active levels are limited to one.

Before cleaning output, `simulationAlreadyComplete()` reads `macroData.csv`,
locates its `load` column, reads through the last nonempty row and compares its
load with `startLoad + totalLoad`. Missing/unreadable data, missing columns and
failed numeric conversion return false. The check does not use manager status,
Slurm accounting, `termination.json` or sampling-target completion.

If complete and not forced, MTS2D throws `SimulationAlreadyComplete`; `main()`
prints the message and returns success. Otherwise a fresh simulation with
`cleanDataPath=true` cleans output rather than loading a dump:

- Root `.vtu`, `.pvd` and `.csv` files are removed. CSV files larger than 100 KiB
  are first copied to a uniquely named backup.
- The `data/` folder is backed up if it has at least ten regular files, then
  removed. This backup threshold differs from the CSV threshold.
- Other root file types, manager records and dump directories are not cleared
  by this function. It does not explicitly remove an old `termination.json`.
- The simulator saves the configuration and creates output paths as needed.

There is no run-directory process lock in the current MTS2D source. Completion
detection is not a concurrent-execution lock. The manager's wrapper provides the
run-wide lock for managed launches.

For a new protocol, fixed/anchored constraints and driven node groups are
prepared. Reversibility adds its CSV columns. For relaxation/reversibility, the
initial state is relaxed by `firstStep()` with temporary single-thread LBFGS,
no preconditioner, ten corrections, strict optimizer tolerances and `epsR=1e-6`.
It applies starting deformation and seeded initial noise, records the initial
state, then restores the requested settings and OpenMP thread count. For
`noRelaxation`, it initializes, applies starting deformation and records that
state without initial relaxation or noise.

## 12. Explicit checkpoint loading and output recovery

Source: `Simulation::loadSimulation()` in
[simulation_outputs.cpp](MTS2D/src/Simulation/simulation_outputs.cpp).

1. Deserialize the supplied `.xml` or decompressed `.xml.gz` state through cereal.
2. Mark solver/output initialization incomplete.
3. If a configuration was supplied, parse it using the checkpoint backend as the
   absent-backend default. Require matching row and column counts.
4. Require the checkpoint's periodic/nonperiodic topology to match the resolved
   configuration. Direct MTS2D restart does not compare a manager configuration
   hash or require the original executable checksum.
5. Apply explicit output-root override. If the saved output path is missing,
   search known storage roots. In nonquiet mode the alternate-path path asks for
   confirmation; quiet mode accepts the alternate path.
6. Load resolved settings, reset seed/thread setup and point mesh output at the
   resolved run name and path.
7. If checkpoint load has reached the current load budget and rerun is not
   forced, report already complete. Otherwise save the resolved configuration.
8. Recover supported CSV columns from existing output. Require the existing CSV
   header to equal the selected output columns before trimming/appending.
9. Back up an existing macro CSV larger than 100 KiB. Trim rows after the loaded
   checkpoint's `load_step`; the normal recovery path preserves its current row.
   A diagnostic recovery can also request removal of that current step.
10. Require valid `load_step` values while scanning, then append to recovered
    output. Initialize solver arrays and refresh mesh geometry, forces,
    averages, plastic-event information and angles.

Dump writing serializes XML to a temporary XML file and compresses into the
final `.gz` path; it does not atomically rename a completed gzip file into place.
Some compression/open failures print diagnostics and return rather than throw.
The manager's latest selection does not validate checkpoint contents before
choosing a candidate, so a partially written candidate can fail during loading.

## 13. Loading, minimization and scientific stopping

Sources: [protocols.cpp](MTS2D/src/Simulation/protocols.cpp),
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

Source: [simulation_outputs.cpp](MTS2D/src/Simulation/simulation_outputs.cpp).

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
wrapper update succeeded.

`main()` maps already-complete detection to exit code zero, a cereal dump-load
exception wrapped as `DumpLoadError` to code 2, and other standard exceptions to
failure. Some argument/config-discovery errors print an error and return from
argument handling, after which `main()` returns zero; wrapper success alone is
not a comprehensive scientific-output validation.

## 15. Wrapper logging, exit handling and cancellation

Sources: `runner.execute()`, `runner.stop()` and
[journal.py](MTSManager/MTS_Management/journal.py).

While MTS2D runs, the wrapper reads combined stdout/stderr in blocks, appends raw
bytes to the attempt's `stdout.log`, and prints lines with a readable
`[label:hash-prefix]` tag. In Slurm/detached mode these wrapper lines reach
`launcher.log`. MTS2D does not create these manager attempt logs by itself.

On simulator exit code zero, the wrapper records `finished`, return code and
finish time. It does not require `termination.json`, inspect target counts or
independently verify convergence. A nonzero exit raises a process error and
records `failed`. SIGTERM/SIGINT or a keyboard interruption records `stopped`.
If the child remains alive during exception handling, the wrapper sends SIGTERM
to its process group, waits up to ten seconds, then uses SIGKILL if needed.

`set_status()` atomically updates status under a lock and appends a journal event.
Transitions away from an already finished state invalidate cached inventory.
The journal is append-only JSONL with event UUID, timestamp and host; writes are
locked, flushed and fsynced. Imported events retain their IDs, duplicates are
deduplicated, and conflicting IDs or malformed records raise errors. Remote
imports use byte cursors that advance only after events are merged.

`stop()` requires an active attempt and checks its saved process/scheduler state:

- With a scheduler ID, it calls `scancel`. Running/stopping jobs remain `stopping`
  until execution ends. If cancellation fails, it queries `sacct` and accepts
  only a recognized terminal scheduler state; otherwise it raises an error.
- For a running local job, it checks process-start tokens before signalling the
  wrapper, preventing accidental signalling of a reused PID. If the wrapper is
  missing, it can terminate the matching simulator process group directly.
- For a prepared detached launch, it can terminate the matching launcher.
- A queued job without a scheduler ID is treated as uncertain and requires
  inspection rather than assuming it can be safely cancelled.

The current MTS2D source has no corresponding scheduler cancellation or
manager-style signal handler that guarantees a checkpoint on SIGTERM. Abrupt
termination or killing the wrapper can leave stale manager status and no final
scientific summary. A continuation can reconcile its predecessor's stale status
using accounting, but routine inventory does not universally repair it.

## 16. Campaign-specific MAGI benchmark controller

Source: [launch.py](MTSManager/campaigns/magi_benchmark_oct26/launch.py).

This controller adds checks beyond generic campaign submission:

1. Loads the strict campaign and its `.resources.json` companion.
2. Requires the entire campaign file checksum to match the resource plan.
3. Requires the recorded diagnostic label, the expected benchmark count plus
   that diagnostic, and resource entries for every system size.
4. A dry run ends after those plan checks; it does not verify a remote build or
   submit jobs.
5. Real submission must run on the execution host using a native Slurm profile,
   not a profile that transports through SSH.
6. Verifies executable identity, the plan's simulator revision and
   `ENABLE_NATIVE_ARCH=OFF`.
7. Acquires the campaign `.submit.lock` nonblockingly. Requires explicit
   `--resume` if submission receipts or indexed runs already exist.
8. Rejects duplicate/unexpected receipts. For indexed runs, checks settings and
   executable, requires exactly one attempt with a scheduler ID, and verifies
   agreement with any existing receipt. Reuses that accepted submission without
   resubmitting it.
9. Rejects a receipt whose run has disappeared. Otherwise submits each missing
   run using size-dependent Slurm resource overrides.
10. Appends/fsyncs each confirmed receipt to `submissions.jsonl`; periodically
    writes `submission.json` progress. Records campaign submission start,
    completion or failure events.

Here `--resume` means resume the **submission controller**, not restart every
simulation from a checkpoint. The controller does not pin one binary by campaign
name: it selects the profile executable, checks the planned revision and checks
existing manifests against its bytes.

## 17. Inventory, GUI monitoring and retrieval

Sources: [inventory.py](MTSManager/MTS_Management/inventory.py),
[inventory_cache.py](MTSManager/MTS_Management/inventory_cache.py),
[status.py](MTSManager/MTS_Management/status.py),
[server.py](MTS_GUI/server.py), `cluster.fetch_many()`.

### 17.1 Execution-host inventory

Missing indexes are rebuilt under `.prepare.lock` by inspecting directories with
manifests, validating configurations/folder names, matching manifest hashes and
rejecting duplicate configuration directories. A hard full refresh rebuilds
the index and invalidates cached rows.

Inventory refreshes serialize under `.inventory.lock`. They validate requested
hash prefixes, reject ambiguous matches and load cache revisions. Finished rows
without queued continuations can be reused without rereading run files during a
normal refresh. Other rows reread configurations, index identity, requests and
statuses. The displayed attempt is the latest running/stopping attempt, otherwise
the earliest prepared/queued attempt, otherwise the latest attempt.

Progress reads a bounded CSV tail, ignores an incomplete final row, and requires
a valid header, correct complete-row width and finite load. The fraction is
clamped to `[0,1]` from load budget; sampling-target completion can therefore
occur below 100% load budget. Non-Slurm running rows also check process identity.
Remote `termination` is exposed only when selected status is `finished` and the
summary exists. Normal inventory does not infer `finished` merely from CSV data.

SQLite publication checks lifecycle revision counters so a concurrent restart
cannot republish an obsolete finished row. Optional size estimates use bounded
traversal/sampling and report incomplete coverage. Hard refresh rereads finished
records without deleting simulation data.

CLI overview/jobs views additionally compare with `squeue`; a recorded job absent
from that queue is labelled for accounting/log inspection, not automatically
declared successful. Slurm may have a terminal state that the wrapper never
recorded.

### 17.2 GUI monitoring

Selecting Monitor data reads local inventory snapshots, campaign definitions and
downloaded copies. It does not itself contact every server or submit jobs.
Campaign definitions are cached by modification time. Downloaded copies validate
configuration against manifest and use the presence of termination data for
their local completion display. Remote and local records are matched by
configuration and manifest checksum; missing campaign members appear explicitly.

Refresh contacts the selected profiles and replaces successful local snapshots;
errors preserve prior snapshots. Campaign refresh uses already discovered source
profiles; it is not a universal search of uncached servers. The GUI operation
log, server inventory and manager journal are distinct records.

### 17.3 Retrieval

`fetch_many()` validates distinct 20–64 digit hash prefixes, reads one remote
inventory and requires unambiguous run selection. `completed_only` skips absent
or nonterminated members explicitly. It requires one output root, rejects local
self/nested copies, and refuses a nonempty destination with a different manifest.

Macro retrieval copies configuration, manifest, macro CSV and termination summary;
full retrieval copies the selected run trees. One rsync transfers the selection.
After transfer, the manager checks manifest bytes and configuration identity,
removes stale local termination summaries when source inventory had none, and
writes `snapshot.json` with retrieval time, site, source root and run checksums.
A macro download does not include the binary, manager journal or attempt logs.

## 18. Ownership, overlap and current limits

| Procedure/check | Manager or scheduler | MTS2D |
| --- | --- | --- |
| Fill new configuration defaults | Current manager defaults | Own defaults for omitted file values |
| Validate protocol/settings | Python type and protocol subset | C++ parsing, backend availability, protocol and numerical checks |
| Verify configuration hash/folder identity | Yes, at read, preparation, execution, inventory and fetch | No manager-hash validation |
| Preserve/verify executable bytes | Retention, build manifest and request checks | No self-check against a manager manifest |
| Reject old configuration schemas | Strict saved readers and build-schema equality | Unknown keys rejected; absent values use defaults or checkpoint backend rule |
| Queue allocations and dependencies | Slurm; manager writes script and explicit job-ID dependencies | None |
| Select latest checkpoint | Wrapper selects greatest `.xml.gz` modification time | No latest search in the current entry point |
| Load physical checkpoint and recover CSV | Supplies explicit `-d` and verifies checksum | Deserializes, checks mesh/topology, trims and appends output |
| Check prior completion | Prevents fresh reuse of a started managed run | CSV load-budget check or supplied checkpoint load check |
| Prevent concurrent writes | Preparation/status locks and run-wide execution lock | No process lock |
| Handle numerical failure/replay | Records resulting process failure | Numerical fallback, eligible LBFGS retry, crash/replay diagnostics |
| Record scientific progress and stopping | Reads output; does not compute scientific targets | Macro CSV, termination counters, final summary and dumps |
| Record allocation/attempt lifecycle | Request/status/journal plus Slurm accounting | Console/scientific output; no manager attempt journal |
| Cancel a job/process tree | Manager stop operation, wrapper signals and Slurm | No equivalent managed cancellation procedure |

Important consequences of the current implementation:

- Retained binaries protect executable selection, but queued jobs still depend
  on the recorded Python installation, current package files, run configuration
  and runtime libraries.
- Updating only manager code can reject a queued old configuration or retained
  binary before MTS2D starts. Recording two manager identities does not bypass
  current schema checks.
- A direct fresh invocation of MTS2D is not a safe substitute for checkpoint
  continuation: an incomplete run can enter output cleanup rather than recovery.
- A scientifically finished sampling run can stop below its load budget. The
  fresh-start CSV completion check does not recognize sampling targets, although
  the protocol and termination summary do.
- A final summary, a zero process exit, a Slurm terminal state and a manager
  `finished` record each establish different facts. They can disagree after
  interruption or an output failure.
- Existing campaign metadata compatibility, direct-binary submission scripts,
  simulator-owned latest selection and simulator-owned process locking are not
  implemented by the source paths described here.

## 19. Source map for maintenance

| Area | Main entry points |
| --- | --- |
| GUI run submission | `Workspace.operation()` action `run`; browser `run-form.onsubmit` |
| CLI dispatch | `cli.main()`; private `_serve` and `_execute` commands |
| Configuration and identity | `Config.__init__`, `_validate`, `read`, `text`, `config_hash`; `sweep` |
| Campaign execution/monitoring readers | `campaign_entries`; `campaign_identity` |
| Site/transport/server dispatch | `load_sites`, `paths`, `call`, `serve`, `submit` |
| Source/build provenance | `source_identity`, `verify_schema`, `build`, `retain_executable`, `executable_identity` |
| Shared installation updates | `deployments.update_managers`; `cluster.deploy`; `install_source` |
| Manager lifecycle | `runner.prepare`, `launch`, `execute`, `set_status`, `stop`, `run_many` |
| Durable history | `Journal.append`, `since`, `merge`; `sync_journals` |
| Simulator entry point | `main.cpp`: `handleInputArgs`, `main` |
| C++ settings | `parseParams`, `initializeConfig`, `parseConfigFile`, `Config::serialize` |
| New/resumed simulation | `Simulation` constructor, `m_loadConfig`, `initialize`, `firstStep`, `loadSimulation` |
| Protocol and stopping | `runSimulationProtocol`; `StepTermination::initialize`, `record`, `reached` |
| Numerical recovery | `Simulation::minimize`, `tryAdjustedMinimization`, `replayMinimizationAfterError` |
| Output/recovery | `saveConfigFile`, `clearOutputFolder`, `simulationAlreadyComplete`, `initCsvFile`, `trimCsvFile` |
| Checkpoints and termination | `saveSimulation`, `m_writeDump`, `finishStep`, `finishSimulation`; cereal helpers |
| Discovery and display | `inventory`, `rebuild_index`, inventory-cache publication; GUI `inventory_overview` |
| Retrieval | `cluster.fetch_many` |

When changing a lifecycle responsibility, review both its submitting path and
its queued execution path, then update the corresponding sections of this
document. A change to the current source does not change the behaviour of an
already retained simulator executable.
