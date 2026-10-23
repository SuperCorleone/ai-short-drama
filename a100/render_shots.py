#!/usr/bin/env python3
"""A100 批渲染器 —— 镜头级断点续跑（PLAN_v2 架构定律 3）。

为什么要断点续跑：rosa 的 A100 排队不可预测，`--time` 是上限不是预留，
作业随时可能被砍或遇到节点故障。若不做断点，每次重排都要从头渲 82 个镜头，
在只有 1-2 张争抢中的卡上这是不可接受的浪费。

契约:
  - 每个镜头是独立工作单元
  - 产出立即落盘，随后**立即**追加一行到 manifest.jsonl
  - 重启时读 manifest，status=ok 的镜头直接跳过
  - 单镜头失败只记录不中断（--max-consecutive-failures 触发才熔断，
    因为连续失败通常意味着环境坏了，继续跑只是烧机时）

用法（由 payload.sh 调起）:
    python3 render_shots.py --stage keyframe --comfy http://127.0.0.1:8189 \
        --out /scratch/gaor/ai_short_drama/outputs/keyframe \
        --shots /home/gaor/ai_short_drama/pipeline/previs/shots.json
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))

# 收到 SIGTERM（Slurm 时限将至会先发 TERM）时优雅收尾，
# 保证 manifest 落盘完整，而不是留下半截状态。
_stop = {"flag": False}


def _on_term(signum, frame):
    print(f"[signal] 收到 {signum}，当前镜头跑完即停，manifest 保持一致", flush=True)
    _stop["flag"] = True


signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)


def post(url, payload, timeout=1800):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_done(manifest_path):
    """读已有 manifest，返回已成功的 shot_id 集合。"""
    done = set()
    if not os.path.exists(manifest_path):
        return done
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 被砍时可能留下半行，跳过
            if rec.get("status") == "ok":
                done.add(rec["shot_id"])
    return done


def run_workflow(comfy, wf, timeout=3600):
    pid = post(f"{comfy}/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            hist = get(f"{comfy}/history/{pid}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"ComfyUI 失联: {e}") from e
        if pid in hist:
            st = hist[pid].get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError(json.dumps(st.get("messages", []), ensure_ascii=False)[:600])
            outs = []
            for node_out in hist[pid].get("outputs", {}).values():
                for key in ("images", "gifs", "videos"):
                    for item in node_out.get(key, []):
                        outs.append(os.path.join(item.get("subfolder", ""), item["filename"]))
            return outs
        time.sleep(2.0)
    raise TimeoutError(f"{pid} 超时 {timeout}s")


def build_workflow(stage, shot, args):
    """按阶段装配 workflow。模板放在 a100/workflows/<stage>.json。

    模板里用 __PROMPT__ / __SHOT_ID__ / __SEED__ / __WIDTH__ / __HEIGHT__ 占位。
    """
    tpl_path = os.path.join(HERE, "workflows", f"{stage}.json")
    if not os.path.exists(tpl_path):
        raise FileNotFoundError(
            f"缺少工作流模板 {tpl_path}。stage={stage} 的模板需在本机验证后再提交到 rosa。"
        )
    raw = open(tpl_path, encoding="utf-8").read()
    repl = {
        "__PROMPT__": json.dumps(shot.get("prompt", ""), ensure_ascii=False)[1:-1],
        "__SHOT_ID__": shot["shot_id"],
        "__SEED__": str(args.seed),
        "__WIDTH__": str(args.width),
        "__HEIGHT__": str(args.height),
    }
    for k, v in repl.items():
        raw = raw.replace(k, v)
    return json.loads(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["bible", "keyframe", "i2v"])
    ap.add_argument("--comfy", default="http://127.0.0.1:8189")
    ap.add_argument("--out", required=True)
    ap.add_argument("--shots", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-consecutive-failures", type=int, default=5,
                    help="连续失败达到此数即熔断（通常意味着环境坏了，继续跑只是烧机时）")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    manifest = os.path.join(args.out, "manifest.jsonl")

    shots = json.load(open(args.shots, encoding="utf-8"))
    if isinstance(shots, dict):
        shots = shots.get("shots", [])
    if args.limit:
        shots = shots[: args.limit]

    done = load_done(manifest)
    todo = [s for s in shots if s.get("shot_id") not in done]
    print(f"[{args.stage}] 总 {len(shots)} 镜头，已完成 {len(done)}，待跑 {len(todo)}", flush=True)
    if not todo:
        print("全部已完成，无事可做")
        return 0

    consecutive = 0
    ok = failed = 0
    with open(manifest, "a", encoding="utf-8") as log:
        for i, shot in enumerate(todo, 1):
            if _stop["flag"]:
                print(f"[stop] 提前退出，剩余 {len(todo) - i + 1} 个镜头留待下次续跑", flush=True)
                break
            sid = shot.get("shot_id", f"shot{i}")
            t0 = time.time()
            rec = {"shot_id": sid, "stage": args.stage, "seed": args.seed,
                   "job_id": os.environ.get("SLURM_JOB_ID"), "ts": time.time()}
            try:
                wf = build_workflow(args.stage, shot, args)
                outs = run_workflow(args.comfy, wf)
                if not outs:
                    raise RuntimeError("工作流无产出")
                rec["status"] = "ok"
                rec["outputs"] = outs
                consecutive = 0
                ok += 1
            except Exception as e:
                rec["status"] = "failed"
                rec["error"] = f"{type(e).__name__}: {e}"[:600]
                consecutive += 1
                failed += 1
            rec["seconds"] = round(time.time() - t0, 1)

            # 立即落盘：作业随时可能被砍，manifest 必须始终是真实进度
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            os.fsync(log.fileno())

            print(f"[{i}/{len(todo)}] {sid} {rec['status']} ({rec['seconds']}s)", flush=True)
            if rec["status"] == "failed":
                print(f"    {rec['error']}", flush=True)
            if consecutive >= args.max_consecutive_failures:
                print(f"[FATAL] 连续失败 {consecutive} 次，熔断退出以免空烧 A100 机时", flush=True)
                return 2

    print(f"\n本轮: 成功 {ok} / 失败 {failed} → {manifest}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
