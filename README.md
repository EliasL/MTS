# MTS

A workspace for mesoscopic tensorial simulations and analysis. It combines three
repositories:

| Project | Purpose | Python import |
|---|---|---|
| [MTS2D](MTS2D/) | C++ simulator | — |
| [MTSManager](MTSManager/README.md) | Configure, run and track simulations locally or on clusters | `MTS_Management` |
| [MTSPlot](MTSPlot/README.md) | Plot simulation responses and performance data | `MTS_Plotting` |

MTSPlot also includes `MTM_Math`, a package for model energy, stress, tangents and
matrix reduction. Each project has its own Git repository; this directory groups
them for local development.

## Setup

Place the repositories beside one another:

```text
MTS/
  MTS2D/
  MTSManager/
  MTSPlot/
```

With Python 3.11 or newer, run from this directory:

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --config-settings editable_mode=compat -e 'MTSManager[mesh]' -e 'MTSPlot[math]'
mts --sites MTSManager/sites.toml build --site local
```

Building requires CMake, a C++17 compiler, OpenMP and zlib. See the
[MTSManager quick start](MTSManager/README.md#installation-and-a-first-run) for
running a small simulation, and the [MTSPlot guide](MTSPlot/README.md) for reading
results and creating figures.

## Development

Open this folder in VS Code and select `.venv/bin/python` as the Python
interpreter. Editable installations link imports to local source files for code
completion and navigation. Workspace tasks provide simulator building and an
example of campaign plotting.

Run each Python project's tests from its own directory:

```sh
MPLBACKEND=Agg ../.venv/bin/python -m unittest discover -s tests -v
```

Keep generated results, virtual environments and build directories out of Git.
Preserve simulation manifests and journals with research data. If you move the
workspace, recreate the environment and build directories to update absolute paths.
