#!/bin/bash
# P2.0 · 权重拉取作业 —— **CPU-only，不申请 A100**
#
# 为什么单独一个作业：rosa 结构上只有 icsnode05/06 各 1 张 A100，且与 tratto 争抢。
# 把几百 GB 的下载放在 GPU 作业里 = 拿最稀缺的资源做最不需要它的事。
# 本作业 --gres=none，跟 GPU 队列完全不冲突，可以先跑、随便跑、跑挂了重跑。
#
# 提交:  sbatch a100/00_fetch_models.sbatch tier1
#        sbatch a100/00_fetch_models.sbatch tier1 tier2
#
#SBATCH --job-name=asd-fetch
#SBATCH --output=/home/gaor/ai_short_drama/logs/fetch_%j.out
#SBATCH --error=/home/gaor/ai_short_drama/logs/fetch_%j.err
#SBATCH --gres=none
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=12:00:00

set -euo pipefail

WORKDIR=/home/gaor/ai_short_drama
SCRATCH=/scratch/gaor/ai_short_drama

mkdir -p "$WORKDIR/logs" "$SCRATCH/models" "$SCRATCH/hf_cache" "$SCRATCH/outputs"

# HF 缓存必须落 /scratch —— /home 是 NFS，只放代码与小输出（TASKS.md rosa 纪律）
export HF_HOME="$SCRATCH/hf_cache"
export HF_HUB_ENABLE_HF_TRANSFER=1

TIERS="${@:-tier1}"
echo "[$(date -Is)] fetching tiers: $TIERS"
echo "[$(date -Is)] node=$(hostname)  dest=$SCRATCH/models"

python3 "$WORKDIR/a100/fetch_models.py" --tiers $TIERS --dest "$SCRATCH/models"

echo "[$(date -Is)] done. disk usage:"
du -sh "$SCRATCH/models" || true
df -h "$SCRATCH" | tail -1
