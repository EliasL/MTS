#!/usr/bin/env bash
set -euo pipefail

MTS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$MTS_ROOT"
export UV_CACHE_DIR="$MTS_ROOT/.mts/cache"
export UV_PYTHON_INSTALL_DIR="$MTS_ROOT/.mts/python"
export MPLBACKEND=Agg
export MPLCONFIGDIR="$MTS_ROOT/.mts/matplotlib"
mkdir -p "$MTS_ROOT/.mts"

MTS_START=0
MTS_LOCAL=0
MTS_TOOLS=0
MTS_OPEN=1
for MTS_ARG in "$@"; do
    case "$MTS_ARG" in
        --start) MTS_START=1 ;;
        --local-sim) MTS_LOCAL=1 ;;
        --install-build-tools) MTS_TOOLS=1 ;;
        --no-browser) MTS_OPEN=0 ;;
        --help)
            printf '%s\n' 'Usage: bash setup.sh [--local-sim [--install-build-tools]] [--start] [--no-browser]'
            exit 0 ;;
        *) printf 'Unknown option: %s\n' "$MTS_ARG" >&2; exit 2 ;;
    esac
done
if [ "$MTS_TOOLS" = 1 ] && [ "$MTS_LOCAL" != 1 ]; then
    printf '%s\n' '--install-build-tools requires --local-sim.' >&2
    exit 2
fi

if [ "$MTS_START" = 1 ] && [ ! -x "$MTS_ROOT/.venv/bin/python" ]; then
    printf '%s\n' 'MTS is not installed. Open InstallationGuide.html, then run bash setup.sh.' >&2
    exit 1
fi
if [ "$MTS_START" != 1 ]; then
    if [ -x "$MTS_ROOT/.mts/tools/uv" ]; then
        MTS_UV="$MTS_ROOT/.mts/tools/uv"
    elif command -v uv >/dev/null 2>&1; then
        MTS_UV="$(command -v uv)"
    else
        printf '%s\n' 'Preparing the Python installer (uv)…'
        if command -v curl >/dev/null 2>&1; then
            curl --fail --location --silent --show-error https://astral.sh/uv/install.sh -o .mts/install-uv.sh
        elif command -v wget >/dev/null 2>&1; then
            wget -q https://astral.sh/uv/install.sh -O .mts/install-uv.sh
        else
            printf '%s\n' 'Install curl or wget, then run this command again.' >&2
            exit 1
        fi
        UV_INSTALL_DIR="$MTS_ROOT/.mts/tools" UV_NO_MODIFY_PATH=1 sh .mts/install-uv.sh
        MTS_UV="$MTS_ROOT/.mts/tools/uv"
    fi
    export MTS_UV
    printf '%s\n' "$MTS_UV" > "$MTS_ROOT/.mts/uv-path"
    if [ ! -x "$MTS_ROOT/.venv/bin/python" ]; then
        "$MTS_UV" venv --python 3.11 "$MTS_ROOT/.venv"
    fi
fi

if [ "$MTS_TOOLS" = 1 ]; then
    "$MTS_ROOT/.venv/bin/python" -m MTS_GUI.install --build-tools
fi
set --
if [ "$MTS_START" != 1 ]; then set -- "$@" --install; fi
if [ "$MTS_LOCAL" = 1 ]; then set -- "$@" --local-sim; fi
if [ "$MTS_OPEN" = 0 ]; then set -- "$@" --no-browser; fi
exec "$MTS_ROOT/.venv/bin/python" -m MTS_GUI "$@"
