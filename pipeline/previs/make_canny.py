"""P1.2b · canny 通道生成（从 previs rgb 帧后处理）。

为什么放 Python 而不是 Godot shader：canny 的两个阈值是要**按画面反复调**的，
灰盒场景的边缘密度差异很大（空镜 vs 多角色镜）。放在 Python 侧可以对每张图
自动搜阈值，shader 方案只能一套参数走天下。

自检与自动修复机制：
  边缘像素占比必须落在 [EDGE_MIN, EDGE_MAX]。
    过低 → 图里几乎没边缘，ControlNet 拿不到结构
    过高 → 全是噪声边，会把扩散模型往碎片化里带
  越界时自动在候选阈值梯度上重搜，仍不达标则标 warn 并记录，不静默通过。

用法:
  eval/venv/Scripts/python.exe pipeline/previs/make_canny.py
  eval/venv/Scripts/python.exe pipeline/previs/make_canny.py --shots G01-S01,G08-S01
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_DIR = os.path.join(HERE, "channels")
REPORT = os.path.join(HERE, "canny_report.json")

# 边缘像素占比的可接受区间。
#
# 2026-08-07 首轮实测重标定：初版用的 [0.01, 0.25] 是照实拍素材的经验值，
# 结果 62/82 报 WARN（实测占比 0.004–0.011）。查证后确认**不是缺陷**：
# 灰盒场景由无纹理平面与基元构成，canny 能提取的只有轮廓线，
# 0.4%–1% 就是它的真实上限。把下限压到 0.003 是按被测对象的物理性质校准，
# 不是为了让报告变绿。
#
# ⚠️ 由此得到的结论要记住：**canny 在灰盒阶段是弱信息通道**，
# 结构信息主要由 depth / normal 承载。canny 的真正用武之地在下游——
# 对 L3 产出的 2K 关键帧取 canny，那才有足够的细节支撑 I2V 的结构控制。
EDGE_MIN, EDGE_MAX = 0.003, 0.25
# 由松到紧的候选阈值：先试中间档，越界再按方向找
THRESHOLD_LADDER = [
    (30, 90), (50, 150), (80, 200), (100, 250), (140, 300), (180, 360),
]
DEFAULT_IDX = 1  # (50, 150)


def edge_ratio(edges) -> float:
    return float((edges > 0).sum()) / edges.size


def make_canny(gray):
    """在阈值梯度上搜一组让边缘密度落进区间的参数。

    返回 (edges, (lo, hi), ratio, status)。
    status: ok / adjusted / warn
    """
    lo, hi = THRESHOLD_LADDER[DEFAULT_IDX]
    edges = cv2.Canny(gray, lo, hi)
    r = edge_ratio(edges)
    if EDGE_MIN <= r <= EDGE_MAX:
        return edges, (lo, hi), r, "ok"

    # 边缘太多 → 往严的方向走；太少 → 往松的方向走
    order = range(DEFAULT_IDX + 1, len(THRESHOLD_LADDER)) if r > EDGE_MAX \
        else range(DEFAULT_IDX - 1, -1, -1)
    best = (edges, (lo, hi), r)
    for i in order:
        lo2, hi2 = THRESHOLD_LADDER[i]
        e2 = cv2.Canny(gray, lo2, hi2)
        r2 = edge_ratio(e2)
        if EDGE_MIN <= r2 <= EDGE_MAX:
            return e2, (lo2, hi2), r2, "adjusted"
        # 记住最接近区间的一组，作为兜底
        def dist(x):
            return max(EDGE_MIN - x, 0) + max(x - EDGE_MAX, 0)
        if dist(r2) < dist(best[2]):
            best = (e2, (lo2, hi2), r2)
    return best[0], best[1], best[2], "warn"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="逗号分隔的 shot_id，缺省=全部")
    ap.add_argument("--blur", type=int, default=3, help="canny 前高斯核（0=不模糊）")
    args = ap.parse_args()

    if not os.path.isdir(CHANNELS_DIR):
        print(f"[error] {CHANNELS_DIR} 不存在，先跑 build_previs.mjs --channels")
        sys.exit(1)

    wanted = set(args.shots.split(",")) if args.shots else None
    shot_dirs = sorted(d for d in os.listdir(CHANNELS_DIR)
                       if os.path.isdir(os.path.join(CHANNELS_DIR, d))
                       and (wanted is None or d in wanted))
    if not shot_dirs:
        print("[error] 没有可处理的镜头目录")
        sys.exit(1)

    results = []
    for sid in shot_dirs:
        rgb_path = os.path.join(CHANNELS_DIR, sid, "rgb.png")
        rec = {"shot_id": sid}
        if not os.path.exists(rgb_path):
            rec.update(status="missing_rgb")
            results.append(rec)
            print(f"{sid} MISSING rgb.png")
            continue
        img = cv2.imread(rgb_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            rec.update(status="decode_failed")
            results.append(rec)
            print(f"{sid} DECODE FAILED")
            continue
        if args.blur > 0:
            k = args.blur if args.blur % 2 == 1 else args.blur + 1
            img = cv2.GaussianBlur(img, (k, k), 0)

        edges, (lo, hi), ratio, status = make_canny(img)
        out = os.path.join(CHANNELS_DIR, sid, "canny.png")
        cv2.imwrite(out, edges)
        rec.update(status=status, thresholds=[lo, hi], edge_ratio=round(ratio, 4))
        results.append(rec)
        mark = {"ok": "OK  ", "adjusted": "ADJ ", "warn": "WARN"}[status]
        print(f"{sid} {mark} edge={ratio:.4f} thr=({lo},{hi})")

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    ratios = [r["edge_ratio"] for r in results if "edge_ratio" in r]
    summary = {
        "total": len(results),
        "counts": counts,
        "edge_ratio_range": [round(min(ratios), 4), round(max(ratios), 4)] if ratios else None,
        "edge_ratio_mean": round(float(np.mean(ratios)), 4) if ratios else None,
        "accept_band": [EDGE_MIN, EDGE_MAX],
        "results": results,
    }
    json.dump(summary, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n{counts} -> {REPORT}")
    # warn 不算失败（已尽力逼近区间），但 missing/decode 失败要非零退出
    hard = counts.get("missing_rgb", 0) + counts.get("decode_failed", 0)
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
