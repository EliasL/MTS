# Regression testing

Tests protect scientific results, saved run identity, restart state and execution
records. A test is useful when a plausible implementation error changes its
observed result. Test counts and assertion counts are not coverage guarantees.

## Running the suites

Use the workspace Python environment:

```sh
.venv/bin/python tools/test.py --build
```

The command builds `MTS2D` and its scientific tests, then runs simulator, manager,
GUI/server and plotting suites. It captures full logs in a temporary directory,
prints suite timings and outcomes, reports skipped tests and returns nonzero on
failure or timeout. No production jobs are submitted and no images are inspected.

A configured Debug build is required for the scientific test executable. From
the workspace root, configure the standard test directory:

```sh
(cd MTS2D && cmake --preset debug)
```

Use `--suite manager`, `--suite simulator`, `--suite gui` or `--suite plot` for
focused work. Repeat `--suite` to select several. `--build-dir` selects a different
configured simulator build and sets `MTS_TEST_BINARY` for executable integration
tests. `--timeout` controls the time allowed per build or suite.

The workspace smoke test checks CLI submission, campaign skip decisions,
inventory and plotted data on an isolated local site. From the workspace root,
select a built simulator to run it directly:

```sh
MTS_TEST_BINARY="$PWD/MTS2D/build/release/MTS2D" \
  .venv/bin/python -m unittest discover -s tests -p test_workspace_smoke.py
```

Run only the suites affected by a change, followed by broader verification when
the change crosses component boundaries. Repeat checks after a relevant change
or failure, rather than repeatedly rerunning successful suites without a reason.

## Coverage by consequence

| Consequence | Main protection |
| --- | --- |
| Scientific formula or derivative error | Independent energy/force/tangent comparisons, frame transformations and finite differences. |
| Invalid mesh or plastic history | Periodic geometry, remeshing, reference/history transformations and Delaunay invariants. |
| Restart changes physical results | Serialization/determinism tests and comparison of resumed physical CSV rows with uninterrupted execution. |
| Configuration identities drift | Fixed fingerprints of the complete October 2026 benchmark/pilot hash sets; saved-config round trips. |
| Metadata silently changes an original run | Editable supplement tests verify changing interpretation, stable hash/file bytes, explicit-value precedence and missing-information errors. |
| Queued jobs import a changed manager | Generated direct scripts actually execute with an unusable Python environment. |
| Chained jobs start incorrectly | Mocked scheduler submission verifies predecessor dependency; actual saved scripts discover checkpoints created after submission. |
| Checkpoint loss or incorrect selection | Reversed file timestamps, corrupt files, folder locking and deterministic publication failure followed by successful recovery. |
| A completed sample is loaded twice | Target completion leaves CSV bytes unchanged; explicit earlier-checkpoint replay reproduces rows and counters. |
| Lost/mixed administrative records | Journal merging, identity checks, cache invalidation, submission uncertainty, retained binaries and retrieval destination checks. |
| Damaged SSH settings or GUI state | Preservation of unrelated profiles/keys, reviewed host-key acceptance, session boundaries and restart persistence. |
| Misleading plotted statistics | Partial/diagnostic-run exclusion, cumulative timing differences, seed weighting, mixed-configuration rejection and physical energy comparisons. |

Slurm is represented by a scheduler fixture; the generated scripts execute real
small simulations locally. This checks submission logic and command execution
without requiring a cluster allocation. It does not test a particular cluster's
module installation, scheduler configuration or filesystem locking behaviour.

## Adding or changing tests

Use a small deterministic example, a saved regression input or an independently
derived physical invariant. Prefer a real outcome such as recovered rows,
unchanged file bytes or a known physical quantity over checking that a helper
was called. Mock external systems where necessary, while executing the code that
contains the consequential behaviour.

Keep numerical parameter combinations when they exercise different branches or
physical constraints. Aggregate repetitive element/image comparisons where the
same invariant applies to all entries, retaining useful failure context and
nonfinite-value detection. Do not reduce an exhaustive test to arbitrary samples
solely to lower the reported assertion count.
