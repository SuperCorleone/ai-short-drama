"""P1.1 代理关键帧生成器：Z-Image-Turbo @ 4070（ComfyUI headless API）。

设计要点（对应 PLAN_v2 架构定律 1「声明式批作业」）：
  - 只走 ComfyUI 的 /prompt HTTP API，不碰 GUI
  - 输入是 shots.json 那种结构化清单，输出是可寻址文件树 + manifest
  - 单镜头失败不中断全局，写进 manifest 便于断点重跑

用法:
  # 冒烟：单条提示词
  comfy/venv/Scripts/python.exe pipeline/keyframe/run_zimage.py --prompt "..." --out smoke

  # 批量：从 shots.json 取镜头
  comfy/venv/Scripts/python.exe pipeline/keyframe/run_zimage.py --shots pipeline/previs/shots.json --limit 5
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l3_prompt  # noqa: E402  提示词模板与本机/A100 共用，见该模块说明

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMFY = "http://127.0.0.1:8189"
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zimage_workflow.json")
OUT_DIR = os.path.join(ROOT, "pipeline", "keyframe", "frames")
COMFY_OUTPUT = os.path.join(ROOT, "comfy", "ComfyUI", "output")


def post_json(path, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        COMFY + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(path):
    with urllib.request.urlopen(COMFY + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def submit(prompt, negative, width, height, steps, cfg, seed, prefix):
    wf = json.load(open(WORKFLOW, encoding="utf-8"))
    wf["4"]["inputs"]["text"] = prompt
    wf["5"]["inputs"]["text"] = negative
    wf["6"]["inputs"]["width"] = width
    wf["6"]["inputs"]["height"] = height
    wf["7"]["inputs"]["steps"] = steps
    wf["7"]["inputs"]["cfg"] = cfg
    wf["7"]["inputs"]["seed"] = seed
    wf["9"]["inputs"]["filename_prefix"] = prefix
    res = post_json("/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})
    return res["prompt_id"]


def wait_result(prompt_id, timeout=900):
    """轮询 /history 直到该任务出现。返回产出的图片相对路径列表。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        hist = get_json(f"/history/{prompt_id}")
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error" or not status.get("completed", True):
                msgs = status.get("messages", [])
                raise RuntimeError(f"ComfyUI 报错: {json.dumps(msgs, ensure_ascii=False)[:500]}")
            imgs = []
            for node_out in entry.get("outputs", {}).values():
                for im in node_out.get("images", []):
                    imgs.append(os.path.join(im.get("subfolder", ""), im["filename"]))
            return imgs
        time.sleep(1.0)
    raise TimeoutError(f"等待 {prompt_id} 超时")


# 短剧成片向的负面词：压掉 AI 味与低质特征（对齐抖音「观感不适/敷衍」红线）
DEFAULT_NEGATIVE = (
    "lowres, blurry, jpeg artifacts, watermark, text overlay, extra fingers, "
    "deformed hands, bad anatomy, oversaturated, plastic skin, cgi look, "
    "flat lighting, cluttered composition"
)


def load_done(manifest_path):
    """读 manifest 取已成功的 shot_id，供断点续跑。

    半行 JSON（写到一半被杀）跳过而不是崩掉 —— 与 a100/render_shots.py 同一套
    约定：只有 `status=ok` 才算完成，其余一律重跑。
    """
    done = set()
    if not os.path.exists(manifest_path):
        return done
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok":
                done.add(rec.get("shot_id"))
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", help="单条提示词（冒烟用）")
    ap.add_argument("--shots", help="shots.json 路径（批量用）")
    ap.add_argument("--limit", type=int, default=0, help="批量时只跑前 N 个镜头（0=全部）")
    ap.add_argument("--out", default="zimage", help="ComfyUI filename_prefix 前缀")
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--height", type=int, default=1344)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--cfg", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--negative", default=DEFAULT_NEGATIVE)
    ap.add_argument("--no-resume", action="store_true",
                    help="忽略已有 manifest，全部重跑")
    ap.add_argument("--only", default="", help="逗号分隔 shot_id，只跑这些")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    manifest_path = os.path.join(OUT_DIR, "manifest.jsonl")
    done = set() if args.no_resume else load_done(manifest_path)

    jobs = []
    if args.prompt:
        jobs.append({"id": "smoke", "prompt": args.prompt, "negative": args.negative})
    elif args.shots:
        shots = json.load(open(args.shots, encoding="utf-8"))
        if isinstance(shots, dict):
            shots = shots.get("shots", [])
        if args.only:
            want = {x.strip() for x in args.only.split(",") if x.strip()}
            shots = [s for s in shots if s.get("shot_id") in want]
        if args.limit:
            shots = shots[: args.limit]
        for s in shots:
            sid = s.get("shot_id", f"shot{len(jobs)}")
            if sid in done:
                continue
            p, neg = l3_prompt.build(s)
            if not p:
                print(f"[skip] {sid} 提示词为空")
                continue
            jobs.append({"id": sid, "prompt": p, "negative": neg})
    else:
        ap.error("需要 --prompt 或 --shots")

    if done:
        print(f"断点续跑：manifest 里已有 {len(done)} 个成功镜头，跳过")
    print(f"待跑 {len(jobs)} 个镜头 @{args.width}x{args.height} {args.steps}步", flush=True)

    results = []
    # **追加**而不是跑完统一写。原实现在全部跑完后才 open(...,"w") 落盘，
    # 82 张要半小时，中途任何中断都会丢掉全部已完成的记录 ——
    # 而文档里写的是"manifest.jsonl 支持断点重跑"。逐条写 + fsync 才对得上。
    with open(manifest_path, "a", encoding="utf-8") as log:
        for i, job in enumerate(jobs):
            t0 = time.time()
            rec = {"shot_id": job["id"], "prompt": job["prompt"], "seed": args.seed,
                   "width": args.width, "height": args.height,
                   "steps": args.steps, "cfg": args.cfg}
            try:
                pid = submit(job["prompt"], job["negative"], args.width, args.height,
                             args.steps, args.cfg, args.seed, f"{args.out}/{job['id']}")
                imgs = wait_result(pid)
                rec["status"] = "ok" if imgs else "no_output"
                rec["images"] = [os.path.join(COMFY_OUTPUT, p) for p in imgs]
            except Exception as e:
                rec["status"] = "failed"
                rec["error"] = str(e)[:400]
            rec["seconds"] = round(time.time() - t0, 2)
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            os.fsync(log.fileno())
            results.append(rec)
            print(f"[{i+1}/{len(jobs)}] {job['id']} {rec['status']} ({rec['seconds']}s)", flush=True)

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n本轮 {ok}/{len(results)} 成功 → {manifest_path}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
