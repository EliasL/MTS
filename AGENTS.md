# Workspace and automation

- The parent repository owns the launcher, browser interface, workspace
  documentation and cross-component checks. `MTS2D`, `MTSManager` and `MTSPlot`
  are independent Git submodules. Check status and instructions in each affected
  repository. When publishing coordinated changes, publish the component commits
  before recording their revisions in the parent.
- Use the [manager API](MTSManager/API.md) for shell and Python automation.
  The GUI and CLI share `submit_run` and `submit_campaign`. Prefer structured
  receipts and inventory records over parsing display text or driving the GUI.
  Check the journal and execution site before retrying an uncertain submission.
- Minimize new SSH connections. For multi-run or long-running operations,
  send one request to the remote MTS manager and let it run the task locally.
  Use durable receipts and infrequent status checks for progress and recovery.
- Keep HTTP and task state in `MTS_GUI/server.py`, monitoring and archives in
  `MTS_GUI/monitor.py`, plot and animation operations in `MTS_GUI/plots.py`,
  and execution logic in MTSManager. Plotting reads local scientific data.
  Remote retrieval belongs to MTSManager and is initiated explicitly.
- Use `MTS_Management.run_records.read_run` to verify the native configuration
  and manifest for analysis. Use `read_run_for_execution` when preparing or
  resuming a run. Preserve configuration, executable and checkpoint identities.
  Keep temporary setting conversions in the recorded-settings reader.
- Run focused checks locally in the affected components. See
  [Testing.md](tests/Testing.md) for the build and test commands.

# Scientific code and testing

Use [Lundheim's thesis summary](docs/ThesisLundheim/Summary.md) and its
page links to check MTM conventions before changing scientific diagnostics.
Distinguish the current lattice Gram matrix from the mesh-edge Gram matrix
and from strain relative to a material reference. For lattice-angle plots,
reconstruct the reduced lattice metric from the saved strain and recorded
constitutive reference.
Verify field definitions at the recorded simulator revision.

Read [TheSimulationProcess.md](docs/TheSimulationProcess.md) before changing
campaign submission, queued execution, configuration/build identity,
checkpoints, lifecycle records or server deployment. Verify the documented
behavior against the source and update the document when lifecycle behavior
changes.

Keep code simple, reuse existing functions, and raise errors for unexpected data
instead of silently inventing behavior.

Present expected, user-correctable failures with actionable guidance in the
interface. Keep diagnostic tracebacks for unexpected failures.

When asked to push, publish completed changes to GitHub's default branch
(`main`), unless another branch is explicitly requested. Publishing only a
feature branch does not complete this request. Preserve existing remote work.

Write README files for newcomers without chat context. Explain what each project
does and how to use it; omit conversation history and implementation chronology.
Introduce names and examples before referring to them, and keep the text concise.

Apply this rule to all user-facing text, including the interface, installation
guide, generated pages and README files. Use brief, formal scientific language.
Document the product and its use; omit explanations answering a particular chat
question, conversational corrections, slogans and speculative future work.
Every statement must be useful to a reader encountering the project independently.

Only add high-value tests that can catch a plausible, consequential future error.
Use tests carefully and with intention: identify the failure a test should catch,
use an expected result independent of the implementation where practical, and
choose the smallest fixture that exposes the failure. Test and assertion counts
are not measures of quality. Prefer checking observable results over mocked call
counts, generated text or assertions that merely repeat the implementation.
Do not add tests merely for coverage, trivial helpers, cosmetic output, routine
argument forwarding, or assertions that restate the implementation. Small changes
often need no new test. Prefer focused regressions for bugs actually encountered.
Apply this policy when reviewing existing tests as well; remove redundant and
low-value tests rather than maintaining them indefinitely.

Keep verification quick and economical in time and tokens. Prefer the smallest
focused test or syntax check that meaningfully verifies a change, with concise
output. Avoid repeated checks, extensive browser verification, screenshots and
broad test suites for routine GUI, management or plotting changes unless a
specific failure or consequential risk justifies them. Minor interface errors
can be identified through user feedback. Choose lower-token verification methods
when they provide sufficient confidence.

The simulator warrants more careful scientific/numerical testing. Preserve its
existing tests; do not apply management/plotting test reductions to MTS2D.

# Element identity and transport

For edge-flip reconnection, an element's identity follows its transported
history. Both long and short transport assign a child to each parent at the
same `eIndex`; the rule chooses the child's geometry. The native VTU writer
emits cells in element-index order. Changing reference-node triplets therefore
does not break transport identity. Triplet matching tracks mesh topology,
not the simulation's transported history; nearest-centroid matching is also
not a replacement for the implemented correspondence.

Verify this contract at the run's recorded source revision. Prefer an explicit
`element_id` when present, checking uniqueness and coverage. Historical native
VTUs can use cell index only after verifying export order and remeshing behavior.
Do not assume that external tools preserve cell order. Scope IDs to one run
and its continuations; they do not identify unchanged material triangles.
If exporting IDs, reuse the existing `eIndex` as `element_id` for this contract,
rather than adding an independent counter or checkpoint state. Include it in
all VTU field levels and both current/reference mesh exports.

Delaunay remeshing currently refills element slots from generated faces; this
is not the edge-flip parent-to-child transport map. Do not describe its slot
indices as geometric transport lineages without a separately verified mapping.
Matrix field names and history updates differ between legacy and current
simulators: inspect the applicable branch instead of copying formulas across.
