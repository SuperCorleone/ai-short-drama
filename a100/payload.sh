#!/bin/bash
# job.sbatch 的实际内容。**排队期间可随意编辑本文件**，作业启动时才读取（R6）。
#
# 2026-08-10 流程（用户选定）：补 VAE → 跑 S2 → 保持 ComfyUI 长驻供注入
#   1. 补齐 MiniMax-H3 缺的两个 VAE（video_vae_fp16 5.21G + audio_vae_fp32 0.61G）
#   2. 把角色圣经参考图从 ComfyUI output/ 同步到 input/（LoadImage 只认 input/）
#   3. 起 ComfyUI 长驻 127.0.0.1:8189
#   4. 跑 S2 · MiniMax-H3 Ref2VA 分层验证（4 镜头：特写/近景/中景/全景）
#   5. **保持 ComfyUI 存活至墙钟**，供 `srun --jobid=<id> --overlap` 注入后续工作
#
# ── 核心设计原则：拿到卡之后绝不主动退出 ──────────────────────────
# 这张卡是排队约 47 小时换来的，FIFO 队列下还回去可能要等一到两天才有下一张。
# 因此**任何一步失败都只降级、不退出**：VAE 没补上就跳过 S2 直接进长驻，
# 让我能注入进来现场修，而不是丢掉卡重新排队。
# 唯一的例外是前置检查失败（连 ComfyUI 或权重目录都不在），那时留着卡也无事可做。

set -uo pipefail   # 故意不设 -e：单步失败不该杀掉整个作业，更不该丢掉卡

WORKDIR=/home/gaor/ai_short_drama
SCRATCH=/scratch/gaor/ai_short_drama
COMFY_DIR="$SCRATCH/ComfyUI"
COMFY_PORT="${COMFY_PORT:-8189}"
PY="$SCRATCH/venv/bin/python3"
STATUS="$WORKDIR/logs/status_${SLURM_JOB_ID:-local}.txt"

# rosa 标准环境前缀（R2 / 设置与配置节，强制）：
#   PYTHONNOUSERSITE — 挡掉 /home/gaor/.local 里他项目的 user-site 包（隔离破口）
#   PIP_CACHE_DIR    — 防 wheel 下载打到 NFS 上的 ~/.cache/pip
#   HF_HOME          — 绝不能指向共享的 /scratch/gaor/hf_cache（那是别的项目的）
export PYTHONNOUSERSITE=1
export PIP_CACHE_DIR="$SCRATCH/pip_cache"
export HF_HOME="$SCRATCH/hf_cache"
export PYTHONIOENCODING=utf-8
export PATH="$SCRATCH/venv/bin:$PATH"

mkdir -p "$WORKDIR/logs" "$SCRATCH/outputs/h3_ref2va"

say() {   # 同时进 stdout 与 status 文件；status 文件小，登录节点可安全 tail（R5 白名单）
    local msg="[$(date -Is)] $*"
    echo "$msg"
    echo "$msg" >> "$STATUS"
}

say "=== 作业启动 node=$(hostname) job=${SLURM_JOB_ID:-none} ==="
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader || true
say "分配: CPU=${SLURM_CPUS_ON_NODE:-?} MEM=${SLURM_MEM_PER_NODE:-?}MB"

# ---- 前置检查（只有这里失败才放弃卡：留着也无事可做）----
for p in "$PY" "$COMFY_DIR/main.py" "$SCRATCH/models/diffusion_models"; do
    if [ ! -e "$p" ]; then
        say "[FATAL] 缺 $p —— 环境不完整，放弃本次分配"
        exit 1
    fi
done

# ============ 步骤 1：补齐 H3 缺的两个 VAE ============
# 2026-08-10 实测：models/vae/vae/ 是空目录，video_vae_fp16 与 audio_vae_fp32 都没落盘。
# 老毛病：H3 仓内路径是 `vae/xxx`，摊平前会落成 models/vae/vae/xxx。
# fetch_models.py 已修（按 basename 外科式摊平），且自带互斥锁 + 空间检查 + max-workers 硬上限。
# max-workers 2 是比纪律（≤4）更保守的值 —— 2026-08-08 并发放大搞挂过 icsnode05。
say "--- 步骤 1/4：补 H3 VAE（已存在的 3 项会 SKIP）---"
timeout 3600 "$PY" "$WORKDIR/a100/fetch_models.py" --tiers h3 --max-workers 2 \
    2>&1 | tee -a "$WORKDIR/logs/fetch_h3_${SLURM_JOB_ID:-local}.log"
FETCH_RC=${PIPESTATUS[0]}          # 取 python 的返回码而非 tee 的（R4.5 #8：管道掩盖退出码）
say "fetch_models 退出码=$FETCH_RC"

VIDEO_VAE="$SCRATCH/models/vae/minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE="$SCRATCH/models/vae/minimax_h3_audio_vae_fp32.safetensors"
VAE_OK=1
for f in "$VIDEO_VAE" "$AUDIO_VAE"; do
    if [ ! -s "$f" ]; then
        say "[WARN] 仍缺 $(basename "$f")"
        VAE_OK=0
    fi
done
# **核验产物而不是信返回码**（R4.5 #8：08-08 被 `ssh|grep` 的退出码坑过两次）
say "VAE 核验: $([ $VAE_OK -eq 1 ] && echo 齐全 || echo 不齐 -- 将跳过 S2 直接进长驻)"

# ============ 步骤 2：同步角色圣经参考图到 ComfyUI input/ ============
# LoadImage 只认 input/ 目录，而派生图生成时落在 output/bible/。
# 2026-08-10 实测：input/bible/ 只有 3 张基准脸，run_h3_ref2va.py 的 REF_SET 里
# 另外 5 张（views_front / expressions_* / outfits_home）全在 output/ 下 → 不同步 S2 必然报文件不存在。
say "--- 步骤 2/4：同步角色圣经参考图 output/bible -> input/bible ---"
mkdir -p "$COMFY_DIR/input/bible"
cp -n "$COMFY_DIR"/output/bible/*.png "$COMFY_DIR/input/bible/" 2>/dev/null
say "input/bible 现有 $(ls -1 "$COMFY_DIR/input/bible" 2>/dev/null | wc -l) 张参考图"

# ============ 步骤 3：起 ComfyUI 长驻 ============
# 危险行为 #4「重复启动同一长驻服务」：起之前先探端口，已有实例就复用。
say "--- 步骤 3/4：启动 ComfyUI :$COMFY_PORT ---"
start_comfy() {
    if curl -sf "http://127.0.0.1:$COMFY_PORT/system_stats" >/dev/null 2>&1; then
        say "端口 $COMFY_PORT 已有健康实例，复用（不重复启动）"
        COMFY_PID=""
        return 0
    fi
    cd "$COMFY_DIR" || return 1
    "$PY" main.py --port "$COMFY_PORT" --listen 127.0.0.1 \
        >> "$WORKDIR/logs/comfy_${SLURM_JOB_ID:-local}.log" 2>&1 &
    COMFY_PID=$!
    for _ in $(seq 1 120); do          # 最多等 10 分钟：冷启要读 20-27GB 权重
        if curl -sf "http://127.0.0.1:$COMFY_PORT/system_stats" >/dev/null 2>&1; then
            say "ComfyUI 就绪 (pid=$COMFY_PID)"
            return 0
        fi
        if [ -n "$COMFY_PID" ] && ! kill -0 "$COMFY_PID" 2>/dev/null; then
            say "[ERROR] ComfyUI 启动即退出，见 logs/comfy_${SLURM_JOB_ID:-local}.log"
            tail -30 "$WORKDIR/logs/comfy_${SLURM_JOB_ID:-local}.log" | tee -a "$STATUS"
            return 1
        fi
        sleep 5
    done
    say "[ERROR] ComfyUI 10 分钟未就绪"
    return 1
}
start_comfy
COMFY_OK=$?

# 作业被 Slurm 砍时（墙钟到点先发 SIGTERM）清理掉 ComfyUI，别留孤儿进程
cleanup() {
    say "收到终止信号，关闭 ComfyUI"
    [ -n "${COMFY_PID:-}" ] && kill "$COMFY_PID" 2>/dev/null
    exit 0
}
trap cleanup TERM INT EXIT

# ============ 步骤 4：S2 · MiniMax-H3 Ref2VA 分层验证 ============
# 4 镜头分层子集（特写/近景/中景/全景），全部含王天薇且有画面描述。
# 分层是为了同时验证 ArcFace 分档门禁：全景的脸像素少，分数天然低于特写，
# 单一阈值会把它误判成身份漂移（P1.3c 已踩过这个坑）。
S2_SHOTS="${S2_SHOTS:-G01-S05,G01-S03,G04-S05,G05-S01}"
if [ $COMFY_OK -eq 0 ] && [ $VAE_OK -eq 1 ]; then
    say "--- 步骤 4/4：S2 H3 Ref2VA · 镜头 $S2_SHOTS ---"
    "$PY" "$WORKDIR/a100/run_h3_ref2va.py" \
        --shots "$S2_SHOTS" \
        --shots-json "$WORKDIR/pipeline/previs/shots.json" \
        --width 832 --height 480 --length 124 --steps 20 \
        --out "$SCRATCH/outputs/h3_ref2va" 2>&1 | tee -a "$STATUS"
    say "S2 退出码=${PIPESTATUS[0]}"
    say "产出: $(ls -1 "$SCRATCH/outputs/h3_ref2va" 2>/dev/null | wc -l) 项"
else
    say "[SKIP] 步骤 4 跳过（comfy_ok=$COMFY_OK vae_ok=$VAE_OK）—— 直接进长驻等注入"
fi

# ============ 长驻：保持 ComfyUI 存活至墙钟，供注入 ============
# 注入方式：srun --jobid=$SLURM_JOB_ID --overlap bash -lc '<命令>'
# 走 scripts/rosa_guard.py 的 heavy()，别再手拼 ssh+srun（R4.5 强制护栏）。
say "=== 进入长驻模式 · ComfyUI :$COMFY_PORT 可用 ==="
say "注入示例: srun --jobid=${SLURM_JOB_ID:-?} --overlap bash -lc 'curl -s 127.0.0.1:$COMFY_PORT/system_stats'"
RESTARTS=0
while true; do
    sleep 300
    if ! curl -sf "http://127.0.0.1:$COMFY_PORT/system_stats" >/dev/null 2>&1; then
        RESTARTS=$((RESTARTS + 1))
        say "[WARN] ComfyUI 无响应，第 $RESTARTS 次重启"
        if [ $RESTARTS -gt 5 ]; then
            say "[ERROR] 连续重启 5 次仍不健康，停止重启（作业继续持卡，等人工注入排查）"
            sleep 3600
            RESTARTS=0
        else
            start_comfy
        fi
    else
        say "心跳 OK · GPU $(nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null | tr -d '\n')"
    fi
done
