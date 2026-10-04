#!/bin/bash
# ============================================================
# run_experiment.sh  —  Launcher de experimentos
# ============================================================
# Lee experiments/<nombre>/config.yml y envía un job SLURM
# por cada modelo definido en la configuración.
#
# Uso:
#   bash extraction/run_experiment.sh InitialTests-full-GLQ
#
# O con variable de entorno:
#   EXPERIMENT=InitialTests-full-GLQ bash extraction/run_experiment.sh
#
# Auto-resume: si ya existen JSONs para un modelo y la cantidad
# alcanza n_epicrisis, ese modelo se omite automáticamente.
# ============================================================

set -eo pipefail

module load micromamba
CONDA_ENV="${CONDA_ENV:-${CONDA_DEFAULT_ENV:-newLLM}}"
export CONDA_ENV
micromamba activate "$CONDA_ENV"

EXPERIMENT="${1:-${EXPERIMENT:-}}"

if [[ -z "$EXPERIMENT" ]]; then
    echo "ERROR: especifica el nombre del experimento."
    echo ""
    echo "Uso:"
    echo "  bash extraction/run_experiment.sh <nombre_experimento>"
    echo ""
    echo "Experimentos disponibles:"
    SCRIPT_DIR_TMP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    ls "$SCRIPT_DIR_TMP/experiments/" 2>/dev/null | sed 's/^/  /' || echo "  (ninguno)"
    exit 1
fi

SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
EXP_DIR="$SCRIPT_DIR/experiments/$EXPERIMENT"
CONFIG_FILE="$EXP_DIR/config.yml"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config.yml no encontrado en:"
    echo "  $CONFIG_FILE"
    exit 1
fi

echo "═══════════════════════════════════════════════════════════"
echo "  run_experiment — Launcher"
echo "═══════════════════════════════════════════════════════════"
echo "  Experimento : $EXPERIMENT"
echo "  Config      : $CONFIG_FILE"
echo "  Script dir  : $SCRIPT_DIR"
echo ""

python3 "$SCRIPT_DIR/extraction/run_experiment.py" \
    --launch \
    --experiment "$EXPERIMENT" \
    --script_dir "$SCRIPT_DIR"
