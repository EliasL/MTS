# The simulation process

This document describes preparing, submitting, resuming and inspecting MTS2D
simulations. Folder execution is recorded as `folder-v1` in build manifests;
retained builds without that interface use their saved worker path. The archived implementation is
documented in [TheSimulationProcess_old.md](TheSimulationProcess_old.md).

## 1. Components and terminology

The user operates the MTS GUI on their own computer. The GUI communicates over
SSH with an MTSManager installation on each cluster. That manager prepares the
remote files and submits commands to the cluster scheduler. Local simulations
use the same preparation and storage conventions with a local manager.

Slurm is the cluster scheduler. It allocates compute resources, starts submitted
scripts, captures their output and records job outcomes. MTS2D is the simulation
executable. It reads a prepared run folder, starts or resumes the simulation,
writes scientific output and prevents simultaneous execution in that folder.

| Term | Meaning |
| --- | --- |
| Configuration | Original saved simulator settings, excluding the generated name. These determine configuration identity. |
| Effective settings | The interpretation used by a reader after applying campaign descriptions of missing or updated settings. |
| Run | An output folder associated with a configuration hash and selected executable checksum. |
| Attempt | One invocation of a run, with its saved command, environment, submission record and separate log. |
| Campaign | A directory containing simulation definitions, resource requests, descriptive metadata and references to its retained binaries. |
| Retained binary | An executable saved with its build record and reused by any number of jobs. |
| Checkpoint or snapshot | A serialized simulation state from which MTS2D can continue. |
| Manifest | A JSON record describing a run or build and its identity. |

Submission acceptance, process exit and scientific completion describe different
events. An accepted job may still be waiting in the queue. A successful process
exit can mean either that a simulation finished or that it was already complete.
The simulator's termination summary identifies the scientific stopping reason.

## 2. Files and identities

Site profiles determine the absolute locations. A campaign has three JSON files;
binary storage and simulation output are shared workspace facilities.

```text
MTSManager/
  sites.toml
  campaigns/<campaign-directory>/
    simulations.json                 # Campaign name and labelled configurations
    resources.json                   # Resource requests and benchmark constraints
    metadata.json                    # Supplemental settings and binary references
  .state/
    job-log.jsonl                    # Submission and administrative events
    manager-deployments.json

execution-workspace/                 # Remote or local
  MTS2D/                             # Source snapshot
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
    manifest.json                    # Original settings and executable association
    .run.lock                        # Held by MTS2D during execution
    macroData.csv
    termination.json                 # Scientific termination summary
    data/
    dumps/
    backups/
    attempts/<attempt-id>/
      request.json                   # Exact command, environment and resources
      submit.sh                      # Saved shell script
      stdout.log                     # Captured stdout and stderr
```

### 2.1 Configuration identity and its purpose

Configuration identity provides a deterministic mapping from settings to an
output folder. Each distinct set of identity settings has a distinct hash, so
simulations with different settings have separate folders. Given a known
configuration, the manager calculates its hash and locates the associated
folder. Going in the other direction requires no hash reversal, since each
folder contains a `config.conf`.

The configuration hash is SHA-256 of canonical JSON containing the original
saved settings:

```json
{"format": "mtscripts-config-v1", "settings": {"...": "original saved settings"}}
```

Canonical serialization sorts keys, uses compact separators and rejects NaN.
The format identifier is stable. Thread count, backend and output settings all
participate. Labels, campaign names, site paths, resource requests and campaign
metadata do not.

Configurations created by the manager explicitly record all supported settings.
An existing configuration retains its original settings and hash when campaign
metadata supplies a description of a subsequently introduced parameter. A
configuration with that parameter recorded explicitly has a different identity,
even if the two configurations describe equivalent physical behaviour.

Folder names contain a readable label and the first 20 hash digits. Manifests
and indexes contain the full hash. The index finds an existing folder even when
the submitted label differs. Labels therefore do not create independent copies
of identical configurations. Independent repetitions with identical settings
use separate output roots. A conflicting truncated hash raises an error.

Labels contain 1–120 ASCII characters, start with a letter or digit and otherwise
use letters, digits, underscores, dots or hyphens.

### 2.2 Manifests and checksums

A run's `manifest.json` records its original configuration, full configuration
hash, campaign and associated executable checksum. The executable's
`build-manifest.json` records how that binary was built: source revision,
build-input digest, compiler/build options and executable checksum.

The two build identities answer different questions:

- **Source identity:** which source revision and build-input files produced the
  build? This supports tracing results to code and reproducing the build.
- **Executable checksum:** are these exactly the same executable bytes? This
  detects replacement or corruption and identifies the binary actually used.

The same source can produce different binaries when compiler versions, flags,
libraries or target architectures differ. Source identity and executable
checksum are therefore recorded together in the build manifest, but are not
merged into a single interchangeable identifier.

| Record | Use |
| --- | --- |
| Configuration hash | Map original settings to the run folder. |
| Executable checksum | Identify and verify the executable bytes. |
| Source identity | Identify the source revision and build inputs. |
| Build-record hash | Name a retained executable directory from its complete build record. |
| Manifest checksum | Check that a transferred manifest matches the source record. |
| Submission manager identity | Record the manager version that prepared the attempt. |

A manifest checksum verifies the manifest file, not all scientific output in
the folder. Download validation also checks the configuration identity.

### 2.3 Campaign references and local binary storage

Campaign metadata lists the retained binary paths used on each execution site,
with their executable checksums. These references provide a convenient way to
find campaign binaries. Each run manifest and saved attempt command records
the binary actually selected for that run; a campaign can refer to several
builds without changing existing attempts.

Remote binaries and their build manifests are also saved locally for retention.
They use the same `executables/<build-record-hash>/` archive as locally built
binaries. Matching retained records are reused, so many jobs and campaigns can
share one copy. Copying verifies the executable checksum and preserves the build
record. A local copy of a remote binary is an archival copy; executing it locally
requires a compatible operating system, architecture and runtime libraries.

The saved command refers to a stable retained path. Working source updates and
working-binary rebuilds leave that retained copy available. The required shared
libraries, modules and environment setup are recorded with the attempt.

## 3. Configuration validation and campaign files

### 3.1 Configuration creation and validation

The manager fills unspecified settings from defaults when constructing a
configuration, then saves a complete configuration. It rejects unknown keys,
invalid types, nonfinite numbers and characters that would break the
configuration-file syntax. Integer values for floating-point settings are
normalized to floats before hashing.

Validation covers:

- Enumerated loading, deformation, boundary, reconnection, minimizer,
  preconditioner, backend, update, energy-function, mesh-diagonal and stopping
  choices.
- Positive dimensions, thread count and LBFGS correction count.
- Nonzero load increment, nonnegative loading budget and segment length, and
  valid signed horizontal/vertical directions.
- Positive segment length for multiple or repeated directions.
- A finite signed `startLoad`, a finite endpoint `startLoad + totalLoad`, and a
  positive loading budget large enough to advance floating-point load when a
  positive budget is requested. Negative starting deformation is permitted.
- Positive reversibility tolerance and preconditioner displacement threshold.
- Nonnegative checkpoint intervals, output counts, iteration limits, residual
  tolerance, disorder/noise amplitudes, energy thresholds and stopping targets.
- Constraints required by the selected physical boundary conditions and any
  requested measurements, such as plastic/elastic classification.

Unusual combinations that are mathematically defined are allowed. Step noise
is available in every loading mode, including without relaxation. For simple
shear, disabling affine prediction
leaves free nodes at their previous positions while the imposed deformation is
applied. This is permitted even without relaxation. Defaults select the usual
combinations; these explicit choices do not require additional rejection rules
or a separate warning system.

MTS2D validates the configuration it reads, including backend availability and
state compatibility. This also applies when it is launched directly, without
the manager. Selecting CHOLMOD in a binary built without CHOLMOD produces an
error.

### 3.2 Existing saved configurations

To deal with existing saved configurations, the original config file is treated
separately from its effective interpretation. This allows an outdated config to
be read by a manager that expects additional or changed settings. The manager
looks in the campaign metadata for the corresponding supplemental information.

The name, hash and run manifest are checked against the original saved settings.
The effective interpretation supplies information for validation, monitoring
and analysis. Missing required information remains an error. The
`missing_settings` entries described below fill absent parameters; an explicitly
saved simulation value takes precedence.

Neither interpretation nor inventory refresh rewrites `config.conf`. MTS2D also
preserves this identity-bearing file when resolving omitted parameters. Resolved
execution information is recorded in simulator output. This rule applies to
configuration changes generally, rather than to one particular parameter.

### 3.3 The three campaign files

The campaign folder contains three JSON files. `simulations.json` describes
which simulations belong to the campaign, `resources.json` describes how they
are submitted, and `metadata.json` provides supplemental descriptions and paths
to retained binaries.

**`simulations.json`** contains the campaign name and labelled configurations:

```json
{
  "name": "MAGI-Benchmark-20261023",
  "runs": [
    {
      "label": "ExampleL100",
      "settings": {"...": "complete original simulation settings"}
    }
  ]
}
```

The example abbreviates the settings object. Duplicate configuration hashes are
errors, even when labels differ. Campaign generation supports Cartesian products
of requested parameter values; the convenience axis `L` sets both dimensions.

**`resources.json`** contains the execution site and resource requests. For
example, this plan requests memory and wall time according to system size:

```json
{
  "site": "magi",
  "resources": {
    "100": {"mem": "4G", "time": "3-00:00:00"},
    "200": {"mem": "8G", "time": "3-00:00:00"},
    "800": {"mem": "56G", "time": "3-00:00:00"}
  }
}
```

The keys `100`, `200` and `800` identify system sizes. `mem` is the memory request;
`time` is the maximum allocation duration in Slurm's days-hours:minutes:seconds
notation. CPU requests come from the simulation thread count and the site's
CPU allocation settings. Site profiles supply other scheduler options such as
partition and account. Benchmark plans can additionally record expected run
counts, diagnostic labels, source revision and a simulation-definition checksum.
That checksum covers simulation definitions, not editable metadata.

**`metadata.json`** contains campaign-wide descriptions and binary references:

```json
{
  "missing_settings": {
    "tangentBackend": "eigen"
  },
  "executables": [
    {
      "site": "magi",
      "path": "/remote/workspace/executables/<build-record-hash>/MTS2D",
      "sha256": "<executable-checksum>"
    },
    {
      "site": "local",
      "path": "/local/workspace/executables/<build-record-hash>/MTS2D",
      "sha256": "<executable-checksum>"
    }
  ]
}
```

The paths and checksums are illustrative. Matching checksum entries can describe
the execution-site binary and its exact local archival copy. Binary references
can also describe distinct builds used by the campaign.

In this example, readers interpret a run that omits `tangentBackend` as using
Eigen. Explicit per-run backend settings take precedence. Metadata can be edited
over time and does not change configuration hashes. Ordinary JSON and effective
setting validation still apply, but metadata is not frozen by a previous use.

The manager, GUI and plotting readers use the same interpretation rules. They
cache campaign information and refresh it when files change. Normal JSON parsing
reads the complete requested file; reading the small metadata file does not
require loading every simulation definition.

## 4. Local GUI and remote manager

The local GUI provides configuration, submission, server updates, monitoring
and retrieval. The user chooses a site profile containing the SSH destination,
working directories, environment setup and scheduler settings.

For remote execution:

1. The local GUI constructs settings from visible fields and explicit overrides.
2. The local manager validates campaign/configuration inputs and sends the
   submission request over SSH.
3. The cluster's manager prepares the run folder, configuration, manifest,
   retained-binary reference and attempt script.
4. The cluster's manager submits that script to Slurm and returns its job ID.
5. The GUI displays the confirmed submission and subsequently reads scheduler
   and simulator records through the remote manager.

The output folder and `config.conf` exist before the main simulation enters the
queue. A successful GUI submission operation means that the job was accepted;
it does not mean that the scientific run has finished.

For local execution, the local manager performs the same preparation and starts
the binary without SSH or Slurm. Foreground execution waits for the process;
detached execution runs independently so the GUI can close while it continues.

## 5. Submission records and the saved command

An attempt records the exact executable path and arguments, working directory,
environment variables, setup/module commands, requested resources and any
scheduler dependency. `request.json` stores this information in machine-readable
form; `submit.sh` preserves the shell invocation. Both are saved under the run's
`attempts/<attempt-id>/` directory before submission.

For example, a command is stored as an argument list rather than reconstructed
later from the current manager's understanding of a binary:

```json
{
  "command": [
    "/absolute/retained/MTS2D",
    "/absolute/output/run",
    "--resume"
  ],
  "cwd": "/absolute/output/run",
  "environment": {"OMP_NUM_THREADS": "16"}
}
```

This is an excerpt from an attempt record. The original invocation remains
available for inspection and repetition. In particular, rerunning a saved
resume command uses the binary's own resume behaviour without translating it
through the current manager's CLI assumptions.

The manager records requested and accepted submissions and preserves uncertain
outcomes. An SSH timeout can occur after the remote scheduler accepted a job.
It therefore does not trigger an automatic resubmission. Scheduler IDs and
submission receipts establish whether an attempt was accepted.

## 6. Source, builds and server updates

### 6.1 Building and retaining binaries

Building is an explicit operation on the execution host. Source identity covers
the source revision and build-input files, including CMake inputs, simulator
source and bundled libraries. Git builds use a recorded mainline revision and
unmodified tracked inputs. Uploaded source snapshots have their own verified
file list and source identity.

The build process:

1. Locks the managed source/build operation against simultaneous source updates.
2. Checks build arguments and rejects a CMake cache belonging to another
   source/build location.
3. Retains the existing working binary and build record before replacement.
4. Verifies source identity and agreement between the source's recognized
   setting names and manager defaults used to create configurations for it.
5. Configures and compiles the executable with recorded build options.
6. Checks that the source did not change during compilation.
7. Writes the build manifest and retains the completed binary.

The retained archive is named from the complete build record. Matching records
reuse an existing verified copy. Creation uses a temporary directory followed by
rename, with read-only executable/manifest permissions. Preparation verifies
executable bytes against their original build record.

Campaign binary references include execution-site paths and local archival
copies. Local archival storage uses the same retention layout and matching-record
reuse as local simulation builds. A later build affects the working executable;
attempts continue to refer to their selected retained paths.

### 6.2 Update MTS on servers

The GUI's **Update MTS on servers** action transfers the manager package,
campaign directories and MTS2D source to their configured locations on each
cluster. It compares manager code, simulator source and campaign definitions
before transfer and skips current profiles. Within an outdated installation,
unchanged components are skipped independently. Matching source records avoid
source archive creation, upload and installation. Enabled SSH destinations update
concurrently using threads; profiles on the same destination update together
and share source uploads for the same workspace. It verifies transfers and
reports errors per server. Both individual and bulk updates refresh the GUI's
connection and version status afterward. The manager CLI supports a forced
update through `mts update-managers --force`; the GUI uses version checks.
Installation records associate site profiles with their remote manager locations.

Source installation verifies archive paths, file lists and source identity,
copies the managed build inputs, removes obsolete listed inputs and verifies
the installed snapshot. Compilation remains a separate operation. Prepared
configuration files, scientific output and retained binary paths remain available
to submitted jobs.

## 7. Preparing a run before queueing

Preparation runs on the execution host:

1. Reads the original configuration and campaign information, resolving
   supplemental descriptions needed for validation and display.
2. Selects the executable and verifies its retained bytes and build record.
3. Resolves the output root and acquires the preparation lock.
4. Calculates the original configuration hash and consults the run index.
   It rejects a conflicting truncated hash or inconsistent manifest.
5. Creates the run folder, `config.conf` and `manifest.json` if absent. For an
   existing run it verifies the saved settings and executable association.
6. Creates the attempt directory and records the exact invocation, environment,
   resources and submission script. Continuation attempts reuse the saved
   invocation appropriate to that binary.
7. Updates the index and records preparation and confirmed submission outcomes.

The original configuration is retained in its run folder. A separate copy per
attempt is not required. A different executable comparison or an independent
identical-settings repetition uses a separate output root.

Preparation is serialized to protect indexes and run creation. MTS2D holds the
execution lock that prevents simultaneous scientific writes. A prepared folder
is not evidence that any simulation process has started.

## 8. Launch commands and job chains

### 8.1 Direct execution

A queued job contains a shell script that calls MTS2D directly. For example:

```sh
#!/bin/bash
set -euo pipefail
# Site setup and required modules
export OMP_NUM_THREADS=16
export OMP_PLACES=cores
export OMP_PROC_BIND=close
exec /absolute/retained/MTS2D /absolute/output/run --resume
```

The configured site supplies setup commands and the actual environment. Slurm
directs stdout/stderr to the attempt log. `exec` replaces the shell with MTS2D,
so the job's process exit status is the simulator's exit status.

Python is used while the manager prepares and submits the script. Starting the
queued job does not start another Python manager process: it starts the retained
executable. Moving or updating the manager package therefore does not change
the queued invocation. The binary, configuration and required runtime
environment remain its execution inputs.

Local foreground execution calls the same executable directly. A detached local
shell launcher captures its log and exit code without requiring the GUI to stay
open. Each attempt retains its command and process/submission identity.

### 8.2 Chaining allocations

A cluster often limits how long one job can run. A simulation that needs more
time uses several jobs in sequence, called a job chain. Each job uses the same
run folder and resume command. When one allocation ends, a later allocation
continues from its latest completed checkpoint.

Jobs for the run share a readable name. Their order is enforced by explicit
scheduler dependencies on predecessor job IDs; the name alone does not serialize
them. With Slurm, `--dependency=afterany:<job-id>` starts a job after its
predecessor has ended, including a timeout or failure. `afterok` instead requires
successful completion. The dependency selected at submission is recorded with
the attempt.

At each start, MTS2D acquires the run lock and processes `--resume`. If the run
is already scientifically complete, it exits successfully. If it has resumable
progress, it continues. An invalid checkpoint produces an explicit error. The
saved script does not query Python code to decide which checkpoint to use.

Cancellation of one allocation and cancellation of the remaining chain are
different scheduler operations. A predecessor dependency determines which
already-submitted successors become eligible; it does not repair numerical
errors or change solver parameters.

## 9. Folder-based startup and configuration

MTS2D accepts the run folder as its argument. That folder supplies both the
configuration location and output destination:

```sh
/retained/MTS2D /absolute/output/run
/retained/MTS2D /absolute/output/run --resume
```

The first form starts a prepared run. The second continues a run or starts it
if it has no checkpoint and no previous scientific output. This permits the
same resume command to serve the first and later allocations of a chain.

Before modifying scientific output, MTS2D:

1. Resolves the folder and requires a readable `config.conf`.
2. Acquires a nonblocking run-directory lock held until process exit. Another
   process targeting the same folder fails explicitly.
3. Parses and validates the configuration, including backend availability.
4. Checks scientific completion using the load budget, termination summary and
   applicable target counters. Sampling completion can occur below the final
   load budget.
5. With `--resume`, selects and loads a checkpoint. If previous progress exists
   without a usable checkpoint, startup fails rather than restarting from zero.
6. Without `--resume`, rejects existing scientific output requiring continuation.
   It does not erase an incomplete run to start again.
7. Restores physical state and recovers output when resuming, or initializes a
   fresh state in a prepared folder without scientific output.
8. Logs the resolved settings and selected checkpoint, preserving `config.conf`.

The folder lock covers discovery, output recovery and execution. Output remains
in the supplied folder. Configuration/checkpoint errors return a nonzero exit;
already-complete detection returns zero with an explicit message.

Explicit configuration, checkpoint and output arguments remain available for
scientific replay. An explicitly supplied checkpoint selects that file rather
than being replaced by automatic resume selection.

### 9.1 Missing settings and backend selection

MTS2D resolves supported omitted parameters according to its configuration and
checkpoint rules without editing the original file. Campaign metadata supplies
administrative readers with the corresponding descriptions for outdated
configurations. The retained binary determines the execution behaviour.

For example, manager-generated configurations explicitly record the tangent
backend, with CHOLMOD as the default. On restart, an omitted backend preserves
the checkpoint's value; checkpoints predating that stored field use Eigen. An
explicit supported configuration value takes precedence.

### 9.2 October 2026 benchmark compatibility

The MAGI benchmark launched in October 2026 uses revision `ed22edb`, an Eigen
binary whose CLI lacks folder-based `--resume`. Its saved jobs invoke a
historical manager worker that selects a checkpoint and passes explicit
configuration, dump and output paths to that binary.

Those benchmark jobs retain their recorded commands and required manager
installation. The saved scripts document the exact launch path. Repeating such
an invocation uses that path rather than translating it into folder-based
syntax. Their run hashes and executable association remain unchanged.

## 10. Checkpoint selection and output recovery

MTS2D writes restart checkpoints to the run's `dumps/` directory. A completed
checkpoint is published only after serialization and compression finish.
Temporary files are excluded from resume discovery; write failures produce
errors. Crash/replay diagnostics are distinct from ordinary restart checkpoints.

Automatic resume selects the ordinary completed checkpoint with the greatest
saved loading-step index. Filesystem modification time does not define physical
progress. Copies of the same saved state are equivalent candidates; conflicting
states at the same greatest index produce an error. The log records the selected
file, load and step. Corruption produces an error rather than an unreported
fallback or restart from zero.

Checkpoint loading:

1. Deserializes physical state and resolved settings.
2. Checks mesh dimensions and boundary topology against the configuration.
3. Preserves saved constraints and reference geometry.
4. Reevaluates scientific completion.
5. Recovers the existing CSV schema and checks column compatibility.
6. Backs up output according to the configured policy, removes CSV rows beyond
   the restored step and opens the recovered file for append.
7. Rebuilds solver state and resumes execution.

Malformed CSV/checkpoint data raises an error. Loading and output recovery are
the same for direct and scheduler-launched MTS2D invocations. The scheduler
captures resume errors in the attempt log and process exit status.

## 11. Scientific output and completion records

During execution, MTS2D writes macro data, configured mesh/diagnostic output and
checkpoints. These files record physical progress independently of the GUI.
Console output is captured in the attempt log. Checkpoint intervals and output
settings determine what state can be recovered after interruption.

A run completes when its loading budget or enabled scientific sampling targets
are satisfied. `termination.json` records the stopping reason, target status
and relevant counters. Sampling-target completion is valid even below the final
load limit. A later resume invocation recognizes that completed state.

The process returns zero for normal completion or already-complete detection,
and nonzero for startup, output, checkpoint or simulation failure. A termination
summary and scheduler exit code provide different evidence: the summary describes
scientific stopping, while the exit code also reflects failures during final
output. Monitoring displays both where available.

Numerical algorithms, solver fallbacks and detailed sampling definitions are
outside this lifecycle description. Numerical errors and diagnostic recovery
remain inside MTS2D and are visible through its output and exit status.

## 12. Logs, history and cancellation

The attempt log contains the simulator's combined stdout/stderr. Its request and
script identify the original command, environment and resources. Slurm supplies
the scheduler ID, allocation history and exit outcome. Local detached launchers
record process identity and an exit-code file when the process ends normally
through the launcher. An interrupted launcher can leave that outcome unknown.

The manager journal records preparation, submission, administrative actions and
retrieval. Scientific history remains in MTS2D output. Monitoring combines these
records without requiring a Python manager to supervise every runtime transition.

Stopping a remote allocation uses `scancel`. Stopping a local process verifies
its saved process-start identity before signalling, avoiding a reused PID.
Remaining queued allocations are cancelled separately when stopping an entire
chain. Historical attempt records are preserved with their logs.

Process exit releases the run lock, including operating-system cleanup after an
abrupt kill. Cancellation does not guarantee a final checkpoint or termination
summary. Subsequent resume uses a previously completed checkpoint. Missing
output is not manufactured as evidence of successful completion.

## 13. Campaign submission and receipts

The campaign controller reads `simulations.json`, `resources.json` and
`metadata.json`. It validates run definitions and available resource requests,
prepares each run folder and submits its saved script. Campaign binary references
are populated from the retained binaries used and their local archival copies.

Benchmark controllers can additionally require expected run counts, diagnostic
labels, portable build options and a recorded source revision. A definition
checksum covers simulation settings separately from editable metadata.

Submission receipts record confirmed job IDs and their run/attempt association.
Resuming an interrupted campaign submission reuses confirmed receipts and checks
existing run manifests instead of resubmitting accepted jobs. Uncertain outcomes
require scheduler/receipt inspection. Receipt continuation is distinct from
MTS2D checkpoint resume.

A campaign is not one scheduler transaction. If a later submission fails, earlier
accepted jobs remain queued and their receipts remain available. Metadata edits
preserve configuration hashes and the commands of accepted attempts.

## 14. Monitoring and inventory

The GUI reads local cached inventories, campaign definitions and downloaded
copies. Refresh contacts remote managers to update the selected server records.
An unavailable server leaves its previous snapshot available with its recorded
update time. Campaign views show both discovered and missing members.

Campaign refresh checks enabled site profiles using the campaign's saved
configuration hashes. It works without an existing server snapshot and sends one
batched selection per profile. The server reads matching indexed runs and reuses
eligible finished records; unrelated jobs are not refreshed. An absent run index
is constructed before the selection is read. Local results merge with unrelated
cached rows. Campaign and full-server refresh times are recorded separately,
and a failed server retains its previous records.

Inventory uses the run index to find folders and validates original
configuration/manifest identity. Campaign metadata supplies effective setting
descriptions. Changes to metadata refresh derived interpretations, without
renaming runs or modifying scientific data.

Prepared/submitted records identify attempts and logs. Slurm state or verified
local process identity describes execution state. CSV data and termination
summaries describe physical progress and scientific completion. A job absent
from the queue is not automatically declared successful; accounting and output
provide the remaining evidence.

CSV inspection reads a bounded tail, ignores a partial row still being written
and rejects malformed complete rows. Load-budget progress and sampling-target
completion are displayed distinctly. Finished records are cached, with lifecycle
and metadata changes invalidating derived data. Hard refresh rereads records
without deleting output. Storage estimates use bounded sampling rather than
unrestricted recursive size scans.

Start simulations and Monitor data include help explaining prepared folders,
configuration hashes, retained binaries, saved commands, resume, job chains and
the distinction between allocation state and scientific completion.

## 15. Retrieval and offline records

The manager selects runs by unambiguous hash prefixes, verifies destination
identity and transfers grouped selections. A nonempty destination with a
different manifest is rejected. After transfer, manifest bytes and original
configuration identity are checked and a retrieval receipt records source site,
time and selected runs.

Macro retrieval includes configuration, manifest, macro data and termination
summary. Full retrieval additionally includes the run tree and attempt logs.
Corresponding campaign files are available locally so supplemental settings and
binary paths can be interpreted without contacting the cluster.

Binary archival copies use the local retained-binary archive described in
section 2.3. Copying scientific output does not itself schedule or execute a run.

## 16. Source references

These files contain the corresponding configuration, lifecycle and simulator
code. The archived document describes their pre-transition execution path.

| Area | Source |
| --- | --- |
| Local GUI and server operations | [app.js](MTS_GUI/static/app.js), [server.py](MTS_GUI/server.py) |
| Configuration and identity | [config.py](MTSManager/MTS_Management/config.py), [defaults.json](MTSManager/MTS_Management/defaults.json) |
| Campaign definitions | [campaign.py](MTSManager/MTS_Management/campaign.py) |
| CLI and remote operations | [cli.py](MTSManager/MTS_Management/cli.py), [cluster.py](MTSManager/MTS_Management/cluster.py) |
| Source/build records and retention | [provenance.py](MTSManager/MTS_Management/provenance.py) |
| Server updates | [deployments.py](MTSManager/MTS_Management/deployments.py) |
| Preparation and submission | [runner.py](MTSManager/MTS_Management/runner.py) |
| Submission history | [journal.py](MTSManager/MTS_Management/journal.py) |
| Inventory | [inventory.py](MTSManager/MTS_Management/inventory.py), [inventory_cache.py](MTSManager/MTS_Management/inventory_cache.py) |
| Simulator CLI and configuration | [main.cpp](MTS2D/src/main.cpp), [param_parser.cpp](MTS2D/src/Data/param_parser.cpp) |
| Startup and loading | [simulation.cpp](MTS2D/src/Simulation/simulation.cpp), [simulation_outputs.cpp](MTS2D/src/Simulation/simulation_outputs.cpp) |
| Configuration/output handling | [data_export.cpp](MTS2D/src/Data/data_export.cpp), [data_csv.cpp](MTS2D/src/Data/data_csv.cpp) |
| Checkpoint serialization | [cereal_help.h](MTS2D/src/Data/cereal_help.h) |
