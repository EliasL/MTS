# Scientific code and testing

Read [TheSimulationProcess.md](TheSimulationProcess.md) for the target design and
[TheSimulationProcess_old.md](TheSimulationProcess_old.md) for the archived
implementation before changing campaign submission, queued execution,
configuration/build identity, checkpoints, lifecycle records or server deployment.
Do not assume target interfaces are implemented; check the source. Keep the
target document's implementation status consistent with lifecycle changes.

Keep code simple, reuse existing functions, and raise errors for unexpected data
instead of silently inventing behavior.

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
