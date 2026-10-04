#!/bin/bash
set -eo pipefail

module load micromamba
CONDA_ENV="${CONDA_ENV:-${CONDA_DEFAULT_ENV:-newLLMfabian2}}"
export CONDA_ENV
micromamba activate "$CONDA_ENV"

EXPERIMENT="${1:-${EXPERIMENT:-}}"
if [[ -z "$EXPERIMENT" ]]; then
    echo "Uso: bash extraction/run_harness_experiment.sh <experimento>"
    exit 1
fi

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONFIG="$SCRIPT_DIR/experiments/$EXPERIMENT/config.yml"
if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: no existe $CONFIG"
    exit 1
fi

python3 "$SCRIPT_DIR/extraction/run_harness_experiment.py" \
    --launch \
    --config "$CONFIG"
