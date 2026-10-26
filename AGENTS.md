# Scientific code and testing

Keep code simple, reuse existing functions, and raise errors for unexpected data
instead of silently inventing behavior.

Write README files for newcomers without chat context. Explain what each project
does and how to use it; omit conversation history and implementation chronology.
Introduce names and examples before referring to them, and keep the text concise.

Only add high-value tests that can catch a plausible, consequential future error.
Do not add tests merely for coverage, trivial helpers, cosmetic output, routine
argument forwarding, or assertions that restate the implementation. Small changes
often need no new test. Prefer focused regressions for bugs actually encountered.
Apply this policy when reviewing existing tests as well; remove redundant and
low-value tests rather than maintaining them indefinitely.

The simulator warrants more careful scientific/numerical testing. Preserve its
existing tests; do not apply management/plotting test reductions to MTS2D.
