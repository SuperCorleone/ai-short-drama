#!/usr/bin/env python3
"""P1 门禁第①项：82 镜头代理关键帧逐张过 L1。

用**生成时用的那条提示词**去算 clip_t —— 从 manifest 读，不重新拼。
重新拼会掩盖问题：如果提示词模板改过，重拼出来的和实际喂给模型的就不是一回事，
算出来的符合度是"模板与图的符合度"而不是"实际提示词与图的符合度"。

阈值取自 TASKS.md §6.2：
  clip_t ≥ 0.20  —— **临时值**，三样本定的，正式标定在 RQ5。这里只作参考不作否决
  musiq  ≥ 55    —— M0 基线 50.16 是 1.3B 草稿档；代理档 Z-Image 应显著高于它
单帧没有 DINOv2（时序）与 RAFT（光流）可言，缺项不算失败。

用法: python pipeline/keyframe/eval_keyframes.py
"""
import json
import os
import statistics
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(ROOT, "pipeline", "keyframe", "frames", "manifest.jsonl")
REPORT = os.path.join(ROOT, "pipeline", "keyframe", "l1_report.md")
EVAL = "http://127.0.0.1:8391"

CLIP_T_MIN = 0.20
MUSIQ_MIN = 55.0


def post(path, payload, timeout=600):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        EVAL + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    last = {}
    with open(MANIFEST, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("status") == "ok" and r.get("shot_id") != "smoke":
                last[r["shot_id"]] = r        # 后写的覆盖先写的 = 取最新一次

    rows = []
    for i, (sid, rec) in enumerate(sorted(last.items()), 1):
        img = rec["images"][0]
        if not os.path.exists(img):
            rows.append({"shot_id": sid, "error": "图片不存在"})
            continue
        try:
            m = post("/eval_image", {"image": img, "prompt": rec["prompt"]})
        except Exception as e:
            rows.append({"shot_id": sid, "error": f"{type(e).__name__}: {e}"[:120]})
            continue
        rows.append({"shot_id": sid, "clip_t": m.get("clip_t"), "musiq": m.get("musiq"),
                     "arcface": m.get("arcface")})
        if i % 20 == 0:
            print(f"  [{i}/{len(last)}]", flush=True)

    ok = [r for r in rows if "error" not in r]
    clip = [r["clip_t"] for r in ok if r.get("clip_t") is not None]
    musiq = [r["musiq"] for r in ok if r.get("musiq") is not None]

    clip_fail = [r for r in ok if r.get("clip_t") is not None and r["clip_t"] < CLIP_T_MIN]
    musiq_fail = [r for r in ok if r.get("musiq") is not None and r["musiq"] < MUSIQ_MIN]

    print(f"\n评估 {len(ok)}/{len(rows)} 张")
    if clip:
        print(f"clip_t : 中位 {statistics.median(clip):.4f}  "
              f"范围 {min(clip):.4f}–{max(clip):.4f}  低于 {CLIP_T_MIN} 的 {len(clip_fail)} 张")
    if musiq:
        print(f"musiq  : 中位 {statistics.median(musiq):.2f}  "
              f"范围 {min(musiq):.2f}–{max(musiq):.2f}  低于 {MUSIQ_MIN} 的 {len(musiq_fail)} 张")

    lines = ["# 82 镜头代理关键帧 · L1 评估报告", "",
             f"- 样本：{len(ok)} 张（Z-Image-Turbo @768×1344，8 步）",
             f"- clip_t 中位 **{statistics.median(clip):.4f}**（阈值 {CLIP_T_MIN}，"
             f"低于阈值 {len(clip_fail)} 张）" if clip else "",
             f"- musiq 中位 **{statistics.median(musiq):.2f}**（阈值 {MUSIQ_MIN}，"
             f"低于阈值 {len(musiq_fail)} 张）" if musiq else "", "",
             "> clip_t 阈值 0.20 是**三样本定的临时值**（TASKS.md §6.2），正式标定在 RQ5。",
             "> 这里只作参考不作否决 —— 拿一个未标定的阈值去否决 82 张图，",
             "> 比不卡更危险，因为它会让人以为已经把过关。", "",
             "## 低于阈值的镜头", "",
             "| 镜头 | clip_t | musiq | 判定 |", "|---|---|---|---|"]
    for r in sorted(set(x["shot_id"] for x in clip_fail + musiq_fail)):
        m = next(x for x in ok if x["shot_id"] == r)
        why = []
        if m.get("clip_t") is not None and m["clip_t"] < CLIP_T_MIN:
            why.append("clip_t 低")
        if m.get("musiq") is not None and m["musiq"] < MUSIQ_MIN:
            why.append("musiq 低")
        lines.append(f"| {r} | {m.get('clip_t')} | {m.get('musiq')} | {'、'.join(why)} |")
    if not (clip_fail or musiq_fail):
        lines.append("| — | | | 全部达标 |")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(x for x in lines if x != "") + "\n")
    with open(os.path.join(ROOT, "pipeline", "keyframe", "l1_scores.json"), "w",
              encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"报告 -> {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
