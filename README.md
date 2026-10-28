# MTS

A local browser workspace for mesoscopic tensorial simulations and analysis.
Configure simulations, inspect run history and logs, and export scientific
figures using a browser interface on macOS or Linux.

## Get started

Clone this repository, then open **InstallationGuide.html** in the MTS folder.
The guide detects your operating system and provides a setup command for the
features you choose:

```sh
git clone https://github.com/EliasL/MTS.git
cd MTS
bash setup.sh
```

Setup downloads missing submodules automatically, prepares a Python 3.11
environment and installs the recorded package versions. Most installation progress
appears in the browser after the initial Python bootstrap. Internet access and
Git are required for first installation. ZIP installation is not supported.

After setup, open **MTS.app** on macOS or **MTS.desktop** on Linux, directly inside
this folder. The launcher starts the local backend and opens your browser. It
reuses an existing session if MTS is already running.
Use **Quit MTS** in the browser to stop the backend; separately managed simulation
jobs continue. `MTS.html` links to the current session and provides startup help.

Linux desktops may require “Allow Launching” for a generated launcher. The
Terminal alternative on either platform is `bash setup.sh --start`.

## Local simulations

The default installation supports plotting and remote simulation management.
To compile and run MTS2D on this computer using existing build tools:

```sh
bash setup.sh --local-sim
```

Optional assisted installation of missing compiler dependencies is available with:

```sh
bash setup.sh --local-sim --install-build-tools
```

On macOS this requests Apple's Command Line Tools when needed, then uses an
existing Homebrew installation for CMake and OpenMP. Finish any Apple dialogs and
rerun the command if instructed. Install Homebrew separately if it is missing.
On Ubuntu/Debian it uses apt and may request an administrator password in Terminal.
Other Linux distributions need a C++17 compiler, CMake, OpenMP and zlib development
files installed manually. The Setup page can rebuild the simulator afterward.
Setup, the manager and the VS Code managed build task share
`MTS2D/build/release/MTS2D`. Generated build directories are ignored by Git;
optional Debug builds live in `MTS2D/build/debug`.

## Project structure

This repository owns the GUI, installation guide and setup script. Three Git
submodules provide the scientific components:

| Component | Purpose | Python import |
|---|---|---|
| [MTS2D](MTS2D/) | C++ simulator | — |
| [MTSManager](MTSManager/README.md) | Configurations, execution, journals and cluster access | `MTS_Management` |
| [MTSPlot](MTSPlot/README.md) | Plotting and model mathematics | `MTS_Plotting`, `MTM_Math` |

The parent repository records compatible component commits. Setup initializes
missing components but preserves populated checkouts and local edits. Updating
the scientific components is an explicit development operation, not a side effect
of opening the application. Remote execution uses the site profiles in
`MTSManager/sites.toml`; review their SSH aliases and resources before submitting.

## Development and troubleshooting

Open the MTS folder in VS Code and select `.venv/bin/python`. Choose
`MTS2D/CMakeLists.txt` if asked for a CMake project, then the `release` preset.
Use **Terminal → Run Build Task → Build managed simulator** (the default build
task) to rebuild and record the executable identity required by managed runs.
On macOS use Apple Clang at `/usr/bin/clang++`.
Personal `.vscode/settings.json`, compiler and launch
settings are ignored; shared tasks and CMake presets are versioned. More editor
guidance is in InstallationGuide.html. Editable installations
link imports to local sources. GUI code and architecture are described in
[MTS_GUI/README.md](MTS_GUI/README.md).

If setup fails, read the browser's operation log. Startup logs are in
`.mts/server.log` and `.mts/launcher.log`. Rerun setup after moving the workspace
to regenerate editable links and launchers. Recreate CMake build directories if
the simulator or manager was moved.

If an unresponsive MTS process holds the server lock, the macOS launcher offers
**Quit and Relaunch**. This may interrupt an active GUI operation. Terminal
launches report the process ID and termination command. Do not delete
`.mts/server.lock`; quitting the process releases the lock automatically.
Other launcher failures display the error and provide an **Open Log** button.

Generated launchers, session files, environments and figures are excluded from
Git. Keep simulation manifests and journals with your research data.
