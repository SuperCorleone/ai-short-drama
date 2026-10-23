#!/usr/bin/env python3
"""A100 侧权重拉取（P2.0）—— 在 **CPU-only 作业** 里跑，绝不占 A100 机时。

rosa 只有 1-2 张 A100 且与 tratto 争抢，把 GPU 机时花在下载上是纯浪费。
本脚本由 `00_fetch_models.sbatch` 以 `--gres=none` 提交。

特性:
  - 分层拉取（tier1/2/3），按需不按满
  - 幂等 + 断点续传（hf_hub_download 自带；已存在则跳过）
  - 扁平化到 ComfyUI models/<类型>/（split_files 目录结构的老坑，见 TASKS.md）
  - 逐文件写 fetch_manifest.jsonl，作业被打断后重新提交即从断点继续

用法（在 rosa 计算节点上）:
    python3 a100/fetch_models.py --tiers tier1
    python3 a100/fetch_models.py --tiers tier1 tier2 --dest /scratch/gaor/ai_short_drama/models
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, "models.json")
DEFAULT_DEST = "/scratch/gaor/ai_short_drama/models"


def _flatten(dest_dir, basename):
    """hf 保留仓内目录结构，把 dest_dir 下任意深度的文件摊平到 dest_dir 根。

    2026-08-08 修：初版只剥 `split_files/`（Comfy-Org 的 Wan/Qwen 仓是这个布局），
    但 Comfy-Org/MiniMax-H3 的路径是 `diffusion_models/xxx`（仓根即类型目录），
    于是文件落成 `models/diffusion_models/diffusion_models/minimax_*.safetensors`
    —— **下载会"成功"，ComfyUI 却完全看不见**。改为按目录名无关的通用摊平。
    `.cache` 是 hf 的断点续传目录，必须原样保留。
    """
    for root, dirs, files in os.walk(dest_dir):
        dirs[:] = [d for d in dirs if d != ".cache"]
        if os.path.abspath(root) == os.path.abspath(dest_dir):
            continue
        if basename in files:
            src = os.path.join(root, basename)
            dst = os.path.join(dest_dir, basename)
            if not os.path.exists(dst):
                shutil.move(src, dst)
            break
    # 只清掉因此产生的空目录，整仓下载留下的目录（如 controlnet/<repo>/）不受影响
    for root, dirs, files in os.walk(dest_dir, topdown=False):
        if os.path.abspath(root) == os.path.abspath(dest_dir) or ".cache" in root:
            continue
        if not os.listdir(root):
            os.rmdir(root)


def fetch_one(entry, dest_root, log, max_workers):
    """单条目拉取。

    一律走 snapshot_download + allow_patterns，**不用 hf_hub_download**：
    rosa 管理员确认压垮登录节点的主因是大文件传输，纪律要求绝不单流拉多 GB。
    snapshot_download 的 max_workers 给出并行分块下载，符合该纪律。
    """
    from huggingface_hub import snapshot_download

    subdir = entry["subdir"]
    dest_dir = os.path.join(dest_root, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    rec = {"repo": entry["repo"], "path": entry["path"], "subdir": subdir, "ts": time.time()}
    t0 = time.time()
    whole_repo = entry["path"] in ("*", "")

    try:
        if whole_repo:
            # 整仓（ControlNet / SeedVR2 / VideoScore2 这类多文件仓）→ 独立子目录，避免文件名撞车
            target = os.path.join(dest_dir, entry["repo"].split("/")[-1])
            if os.path.isdir(target) and os.listdir(target):
                rec["status"] = "skip"
            else:
                snapshot_download(repo_id=entry["repo"], local_dir=target, max_workers=max_workers)
                rec["status"] = "ok"
            rec["dest"] = target
        else:
            basename = os.path.basename(entry["path"])
            final = os.path.join(dest_dir, basename)
            if os.path.exists(final):
                rec["status"] = "skip"
            else:
                # 单文件也走 snapshot + allow_patterns，拿到并行分块
                pattern = entry["path"] if entry["path"].endswith((".safetensors", ".gguf")) \
                    else f"*{entry['path']}*"
                snapshot_download(repo_id=entry["repo"], local_dir=dest_dir,
                                  allow_patterns=[pattern], max_workers=max_workers)
                _flatten(dest_dir, basename)
                rec["status"] = "ok"
            rec["dest"] = final
            if os.path.exists(final):
                rec["gb"] = round(os.path.getsize(final) / 1e9, 2)
            else:
                rec["status"] = "failed"
                rec["error"] = f"下载后未找到目标文件 {final}（检查 models.json 里的 path 是否与仓内路径一致）"
    except Exception as e:
        rec["status"] = "failed"
        rec["error"] = f"{type(e).__name__}: {e}"[:400]

    rec["seconds"] = round(time.time() - t0, 1)
    log.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.flush()
    os.fsync(log.fileno())
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", nargs="+", default=["tier1"], help="tier1 / tier2 / tier3")
    ap.add_argument("--dest", default=os.environ.get("MODELS_ROOT", DEFAULT_DEST))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-workers", type=int, default=2,
                    help="并行分块数。事故后默认从 4 降到 2，且硬性封顶 4——"
                         "并发放大是 2026-08-08 搞挂 icsnode05 的直接原因")
    args = ap.parse_args()
    if args.max_workers > 4:
        print(f"[abort] --max-workers={args.max_workers} 超过硬性上限 4。"
              f"并发放大是搞挂节点的直接原因，这个上限不接受覆盖。")
        sys.exit(6)

    # 隔离自检：HF_HOME 必须落在本项目自己的 scratch 内，
    # 绝不能指到 /scratch/gaor/hf_cache（那是别的项目的共享缓存）
    hf_home = os.environ.get("HF_HOME", "")
    if hf_home and "ai_short_drama" not in hf_home:
        print(f"[FATAL] HF_HOME={hf_home} 不在本项目目录内 —— 会污染其他项目的缓存。")
        print("        应设为 /scratch/gaor/ai_short_drama/hf_cache")
        sys.exit(3)

    # 单实例互斥锁。2026-08-08 教训：手动起了一次下载后看门狗又起了第二次，
    # 远端出现 5 个 fetch 进程 × 每个 max_workers=4 ≈ **20 条并行流**写 /scratch —— 
    # 我按进程遵守了「max_workers=4」，却让**进程数**失控，实际放大 5 倍。
    # icsnode05 此前连续健康 1 天 21 小时，加上这个负载后转 mixed*。
    # 并发上限必须在进程层面也守住，不能只守住单进程内部。
    lock_path = os.path.join(args.dest, ".fetch.lock")
    os.makedirs(args.dest, exist_ok=True)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)
    except FileExistsError:
        try:
            holder = open(lock_path, encoding="utf-8").read().strip()
        except Exception:
            holder = "?"
        print(f"[abort] 已有下载在跑（锁 {lock_path}，pid={holder}）。"
              f"若确认是残留锁，删掉该文件再试。")
        sys.exit(4)
    import atexit
    atexit.register(lambda: os.path.exists(lock_path) and os.remove(lock_path))

    spec = json.load(open(SPEC, encoding="utf-8"))
    entries = []
    for t in args.tiers:
        if t not in spec["tiers"]:
            print(f"[error] 未知 tier: {t}（可选 {list(spec['tiers'])}）")
            sys.exit(1)
        entries.extend(spec["tiers"][t]["files"])

    total_gb = sum(e.get("gb", 0) for e in entries)
    print(f"拉取 {len(entries)} 项，约 {total_gb:.0f} GB → {args.dest}", flush=True)

    # 危险行为 #5：/scratch 是**全集群共享**的 15T，写满会波及所有人，
    # 而本项目 tier1+2+3+h3 合计 250GB+。下载前必须查空间并留足余量。
    try:
        st = os.statvfs(args.dest)
        free_gb = st.f_bavail * st.f_frsize / 1e9
        need_gb = total_gb * 1.15 + 100     # 15% 余量 + 给别人留 100GB
        print(f"空间检查：可用 {free_gb:.0f} GB，需要 ≥{need_gb:.0f} GB"
              f"（含 15% 余量 + 给其他项目预留 100 GB）", flush=True)
        if free_gb < need_gb:
            print(f"[abort] /scratch 空间不足。这是共享文件系统，写满会影响所有人。")
            sys.exit(5)
    except (AttributeError, OSError) as e:
        print(f"[warn] 空间检查跳过（{type(e).__name__}）——非 POSIX 或路径不可达", flush=True)
    if args.dry_run:
        for e in entries:
            print(f"  {e.get('gb', '?'):>7} GB  {e['repo']} :: {e['path']}")
        return

    os.makedirs(args.dest, exist_ok=True)
    manifest = os.path.join(args.dest, "fetch_manifest.jsonl")
    ok = skipped = failed = 0
    with open(manifest, "a", encoding="utf-8") as log:
        for i, e in enumerate(entries, 1):
            rec = fetch_one(e, args.dest, log, args.max_workers)
            mark = {"ok": "OK", "skip": "SKIP", "failed": "FAIL"}[rec["status"]]
            print(f"[{i}/{len(entries)}] {mark:<4} {e['repo']} :: {os.path.basename(e['path'])} "
                  f"({rec['seconds']}s)", flush=True)
            if rec["status"] == "failed":
                print(f"        {rec['error']}", flush=True)
                failed += 1
            elif rec["status"] == "skip":
                skipped += 1
            else:
                ok += 1

    print(f"\n新拉 {ok} / 已存在 {skipped} / 失败 {failed}  → {manifest}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
