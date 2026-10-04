#!/bin/bash
#SBATCH --job-name=f199v2
#SBATCH --nodelist=ih-condor
#SBATCH --partition=batch
#SBATCH --qos=batch
#SBATCH --time=06:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gpus=2
#SBATCH --cpus-per-task=10
#SBATCH --mem=64G
#SBATCH --output=/path/to/workspace/experiments/concordancia50-form199-v2.6/slurm-%j.out
#SBATCH --error=/path/to/workspace/experiments/concordancia50-form199-v2.6/slurm-%j.err
set -eo pipefail
umask 077
# sbatch from a non-interactive shell does not inherit the `module` function.
source /opt/environment-modules/current/init/bash
module load micromamba
micromamba activate vllm-form199
: "${MODEL:?Set MODEL to Gemma4, Llama-70B or Qwen}"
# CODE_DIR: frozen copy of this directory, so edits here cannot change a running chain.
HERE=${CODE_DIR:-${REPO_DIR:-$PWD}/harness}
ARGS=(--run --model "$MODEL" --arm "${ARM:-all}")
# CASES: space-separated patient_ids; otherwise the first CASE_LIMIT cases.
if [ -n "${CASES:-}" ]; then for c in $CASES; do ARGS+=(--case-id "$c"); done; else ARGS+=(--limit "${CASE_LIMIT:-1}"); fi
[ "${EQUIVALENCE:-0}" = "1" ] && ARGS+=(--equivalence)

echo "job=$SLURM_JOB_ID model=$MODEL arm=${ARM:-all} equivalence=${EQUIVALENCE:-0} start=$(date -Is)"
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv
nvidia-smi topo -m | head -4
python3 -c "import sys,torch,transformers,vllm;print('python',sys.version.split()[0],'torch',torch.__version__,'cuda',torch.version.cuda,'transformers',transformers.__version__,'vllm',vllm.__version__)"
sha256sum "$HERE"/batch_driver.py "$HERE"/engine_vllm.py "$HERE"/runner.py "$HERE"/protocol.py "$HERE"/monolithic.py "$HERE"/arm_c.py "$HERE"/prompts/case_model.md
python3 "$HERE"/batch_driver.py "${ARGS[@]}"
echo "end=$(date -Is)"
