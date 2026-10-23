#!/usr/bin/env python3
"""S1 · SageAttention 开关对照基准（Qwen-Image-2512 关键帧）。

方法学要点：
  - 第一次运行会加载 30GB 权重，必须**丢弃**，只取之后 N 次的中位数
  - 每次换 seed，避免任何缓存把结果做平
  - 同时记录产出路径，供后续 CLIP-T / MUSIQ 质量比对
    （只比速度不比质量就是自欺——加速节点最常见的代价是画质下降）

用法:
    python3 a100/bench_keyframe.py --tag nosage --runs 3
    python3 a100/bench_keyframe.py --tag sage   --runs 3
"""
import argparse
import json
import os
import statistics
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, "workflows", "keyframe.json")
COMFY = "http://127.0.0.1:8189"

PROMPT = ("深夜的老旧厨房，暖黄吊灯下，一位二十岁出头的亚洲年轻女性侧身站在灶台前"
          "低头调试一台半人高的料理机器人，机器人外壳有划痕与油污，中景，浅景深，"
          "电影感打光，竖屏构图，写实人像，高细节")
NEGATIVE = ("lowres, blurry, jpeg artifacts, watermark, text overlay, extra fingers, "
            "deformed hands, bad anatomy, oversaturated, plastic skin, cgi look, flat lighting")


def post(path, payload, timeout=3600):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        COMFY + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=120):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run_once(seed, tag, w, h, timeout=3600):
    raw = open(WORKFLOW, encoding="utf-8").read()
    raw = (raw.replace("__PROMPT__", json.dumps(PROMPT, ensure_ascii=False)[1:-1])
              .replace("__NEGATIVE__", json.dumps(NEGATIVE, ensure_ascii=False)[1:-1])
              .replace("__WIDTH__", str(w)).replace("__HEIGHT__", str(h))
              .replace("__SEED__", str(seed))
              .replace("__SHOT_ID__", f"bench_{tag}_s{seed}"))
    wf = json.loads(raw)
    t0 = time.time()
    pid = post("/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})["prompt_id"]
    while time.time() - t0 < timeout:
        h_ = get(f"/history/{pid}")
        if pid in h_:
            st = h_[pid].get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError(json.dumps(st.get("messages", []), ensure_ascii=False)[:600])
            outs = [o for n in h_[pid].get("outputs", {}).values() for o in n.get("images", [])]
            return round(time.time() - t0, 2), (outs[0] if outs else None)
        time.sleep(1.5)
    raise TimeoutError(tag)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="nosage / sage")
    ap.add_argument("--runs", type=int, default=3, help="计入统计的次数（不含预热）")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--out", default="/scratch/gaor/ai_short_drama/outputs/bench")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    print(f"[{args.tag}] 预热一次（加载权重，丢弃计时）…", flush=True)
    warm, _ = run_once(9000, f"{args.tag}_warmup", args.width, args.height)
    print(f"[{args.tag}] 预热 {warm}s（不计入）", flush=True)

    times, outs = [], []
    for i in range(args.runs):
        seed = 9001 + i
        t, out = run_once(seed, args.tag, args.width, args.height)
        times.append(t)
        outs.append(out)
        print(f"[{args.tag}] run{i + 1} seed={seed} {t}s", flush=True)

    res = {
        "tag": args.tag, "warmup_s": warm, "times": times,
        "median_s": statistics.median(times),
        "mean_s": round(statistics.mean(times), 2),
        "resolution": [args.width, args.height],
        "outputs": outs,
    }
    dest = os.path.join(args.out, f"{args.tag}.json")
    json.dump(res, open(dest, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[{args.tag}] 中位 {res['median_s']}s  均值 {res['mean_s']}s -> {dest}")


if __name__ == "__main__":
    main()
