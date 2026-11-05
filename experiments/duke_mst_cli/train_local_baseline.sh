#!/usr/bin/env bash
# Train one matched baseline of the breast MRI worked example (docs/reproduce.md, Baselines).
#
# Runs MediSwarm's training code in local-training mode, in the image of the swarm runs and with
# the same settings, on one site's data, then writes the last and the best checkpoint as
# FL_global_model.pt files that export_global_predictions.py scores like a swarm model.
#
#   train_local_baseline.sh <site> <run> <epochs> <data root> <work dir>
#
#   site       node_A, node_B, node_C, or node_all (the three sites pooled: the centralised model)
#   run        repetition number (1, 2, 3)
#   epochs     120 (node_A), 160 (node_B), 200 (node_C) or 60 (node_all) in the recorded runs
#   data root  folder holding <site>/data_unilateral and <site>/metadata_unilateral; mounted read-only
#   work dir   receives logs/, runs/ and models/<name>_{last,best}/FL_global_model.pt
#
# Example: train_local_baseline.sh node_A 1 120 /mnt/data/DUKE_iid /mnt/data/matched_baselines
#
# The recorded runs used this script with a final step, left out here, that copied the two
# checkpoints to the scoring host and scored them there.
set -euo pipefail

ARM="${1:-}"
REP="${2:-}"
NUM_EPOCHS="${3:-}"
DATA_ROOT="${4:-}"
SCRATCH_DIR="${5:-}"

if [ -z "$ARM" ] || [ -z "$REP" ] || [ -z "$NUM_EPOCHS" ] || [ -z "$DATA_ROOT" ] || [ -z "$SCRATCH_DIR" ]; then
  echo "Usage: $0 <site> <run> <epochs> <data root> <work dir>" >&2
  echo "Example: $0 node_A 1 120 /mnt/data/DUKE_iid /mnt/data/matched_baselines" >&2
  exit 1
fi

# The image of the swarm runs (odelia:paper-apr2026).
TRAIN_DIGEST="sha256:35b600ea5b8aab58f5c2fa5a59e29e1aed864cc301a2f3b4a2b286aeb4858aa6"
MID_BASE="matched_local_${ARM}_rep${REP}"
if [ "$ARM" = "node_all" ]; then
  MID_BASE="matched_centralised_rep${REP}"
fi

LOG_FILE="${SCRATCH_DIR}/logs/${MID_BASE}_train.log"
mkdir -p "${SCRATCH_DIR}/logs" "${SCRATCH_DIR}/runs" "${SCRATCH_DIR}/models"

echo "=== [$(date -Is)] Starting ${MID_BASE}: ARM=${ARM}, REP=${REP}, EPOCHS=${NUM_EPOCHS} ===" | tee -a "$LOG_FILE"

# 1. Pull the image by digest and tag it locally, so that a host that prunes images keeps it.
docker pull "jefftud/odelia@${TRAIN_DIGEST}" >> "$LOG_FILE" 2>&1
docker tag "jefftud/odelia@${TRAIN_DIGEST}" odelia:paper-apr2026 >> "$LOG_FILE" 2>&1

CONTAINER="train_${MID_BASE}"
docker rm -f "$CONTAINER" 2>/dev/null || true

# 2. Train. MediSwarm's main.py in local-training mode calls the training functions that the
# swarm clients call (same model, data module and settings), in one continuous run without NVFlare.
echo "Running container ${CONTAINER}..." | tee -a "$LOG_FILE"
docker run --name "$CONTAINER" --gpus all --shm-size 16g --entrypoint python \
  -e TRAINING_MODE=local_training \
  -e SITE_NAME="${ARM}" \
  -e INSTITUTION="${ARM}" \
  -e NUM_EPOCHS="${NUM_EPOCHS}" \
  -e MODEL_NAME=MST \
  -e CONFIG=unilateral \
  -e DATA_DIR=/data \
  -e SCRATCH_DIR=/scratch \
  -v "${DATA_ROOT}":/data:ro \
  -v "${SCRATCH_DIR}":/scratch \
  -w /MediSwarm/application/jobs/ODELIA_ternary_classification/app/custom \
  odelia:paper-apr2026 main.py 2>&1 | tee -a "$LOG_FILE"

echo "=== [$(date -Is)] Training finished for ${MID_BASE} ===" | tee -a "$LOG_FILE"

# 3. The newest run directory of this site.
RUN_DIR=$(ls -td "${SCRATCH_DIR}/runs/${ARM}/"* | head -1)
echo "Run directory: $RUN_DIR" | tee -a "$LOG_FILE"

# 4. Write the last and the best checkpoint in the layout of a swarm model.
MID_LAST="${MID_BASE}_last"
MID_BEST="${MID_BASE}_best"

mkdir -p "${SCRATCH_DIR}/models/${MID_LAST}" "${SCRATCH_DIR}/models/${MID_BEST}"

docker run --rm --entrypoint python \
  -v "${RUN_DIR}":/run:ro \
  -v "${SCRATCH_DIR}/models":/out \
  odelia:paper-apr2026 -c "
import sys, json, torch
from pathlib import Path

run_dir = Path('/run')
out_last_pt = Path('/out/${MID_LAST}/FL_global_model.pt')
out_best_pt = Path('/out/${MID_BEST}/FL_global_model.pt')
out_info = Path('/out/${MID_BASE}_info.json')

# Convert last
last_ckpt = run_dir / 'last_global_model.ckpt'
if not last_ckpt.exists():
    last_ckpt = run_dir / 'last.ckpt'
ck_last = torch.load(last_ckpt, map_location='cpu', weights_only=False)
torch.save({'model': ck_last['state_dict']}, out_last_pt)

# Convert best
best_json = run_dir / 'lightning_logs' / 'version_0' / 'best_checkpoint.json'
best_ckpt_file = None
if best_json.exists():
    try:
        best_ckpt_file = json.loads(best_json.read_text()).get('best_model_epoch')
    except Exception as e:
        print('Error reading best_checkpoint.json:', e)

best_path = run_dir / best_ckpt_file if best_ckpt_file and (run_dir / best_ckpt_file).exists() else None
if best_path is None:
    ckpts = sorted(run_dir.glob('epoch=*.ckpt'))
    best_path = ckpts[-1] if ckpts else last_ckpt

ck_best = torch.load(best_path, map_location='cpu', weights_only=False)
torch.save({'model': ck_best['state_dict']}, out_best_pt)

info = {
    'mid_base': '${MID_BASE}',
    'run_dir': str(run_dir),
    'last_epoch': ck_last.get('epoch'),
    'last_step': ck_last.get('global_step'),
    'best_file': best_path.name,
    'best_epoch': ck_best.get('epoch'),
    'best_step': ck_best.get('global_step'),
    'n_tensors': len(ck_last['state_dict'])
}
out_info.write_text(json.dumps(info, indent=2))
print('Checkpoint conversion info:', json.dumps(info))
" | tee -a "$LOG_FILE"

echo "=== [$(date -Is)] ${MID_BASE} complete: ${SCRATCH_DIR}/models/${MID_LAST} and ${MID_BEST} ===" | tee -a "$LOG_FILE"
