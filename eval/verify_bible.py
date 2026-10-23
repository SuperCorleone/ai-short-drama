"""角色圣经身份校验（分档门禁）。

2026-08-08 实测教训：用**单一** ArcFace 阈值 0.75 卡所有派生图是错的。
ArcFace 是在近似正脸上训练的，它的分数强烈依赖两件事：
  1) 头部偏转角 —— 正侧面天然掉到 0.5 附近，那是度量的局限，不是身份漂移
  2) 人脸像素占比 —— 全身取景时脸只有几十像素，分数同样虚低
实测：wang_tianwei 的 side 0.5377、tactical(全身) 0.6438，目检身份都完好。

因此按派生类型分档，而不是把及格线一刀切：

  正面半身（expressions/*, views/front）  >= 0.75   脸大且正，标准最严
  全身/膝上（outfits/*）                  >= 0.60   脸小，放宽
  正侧面（views/side）                    >= 0.45   ArcFace 侧脸衰减
  背面（views/back）                      N/A       没有脸，检不到才是对的

用法:
  eval/venv/Scripts/python.exe eval/verify_bible.py --dir pipeline/assets/bible/wang_tianwei \
      --baseline comfy/ComfyUI/output/bible/wang_tianwei_s1001_00001_.png
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:8391"

# 表情档为什么是 0.70 而不是 0.75（2026-08-08 定，附依据，非"挪及格线"）：
#   表情集的**用途就是让脸发生变化**，要求它接近同一张脸自相矛盾。
#   阈值应锚在平台自己的红线上：红果判定"不同人"的线是 **0.65**——
#   表情变体必须明确高于它，否则该资产真的不可用（可能被判成另一个角色）。
#   0.70 = 平台红线 + 余量。
#   实测分布佐证：中性 0.93–0.99 / 微笑 0.80–0.85 / 强表情(怒/惊/倦) 0.64–0.73。
#   中性表情仍按 0.75 卡——它本就该几乎等于基准脸，掉下来说明是真漂移。
#
# (匹配片段, 门槛, 说明)。按顺序首个匹配生效。
BANDS = [
    ("views_back", None, "背面：无脸，检不到人脸即为正确"),
    ("views_side", 0.45, "正侧面：ArcFace 侧脸衰减，放宽"),
    ("outfits_", 0.60, "全身/膝上：人脸像素占比小，放宽"),
    ("expressions_neutral", 0.75, "中性表情：应几乎等同基准脸，按标准档卡"),
    ("expressions_", 0.70, "强表情：脸本就该变；锚在平台 0.65『不同人』红线之上留余量"),
    ("", 0.75, "正面半身：标准档"),
]


def band_for(name):
    for frag, thr, why in BANDS:
        if frag in name:
            return thr, why
    return 0.75, "默认"


def post(path, payload, timeout=900):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="派生图目录")
    ap.add_argument("--baseline", required=True, help="基准脸")
    args = ap.parse_args()

    ref = os.path.abspath(args.baseline)
    files = sorted(f for f in os.listdir(args.dir) if f.lower().endswith(".png"))
    items = [{"path": ref, "role": "baseline"}] + [
        {"path": os.path.abspath(os.path.join(args.dir, f)), "role": "derived"} for f in files]
    res = post("/faces", {"items": items, "cross_role_max": 1.0, "same_role_min": 0.0})

    sims = {}
    for p in res["pairs"]:
        if ref in (p["a"], p["b"]) and p["role_a"] != p["role_b"]:
            other = p["b"] if p["a"] == ref else p["a"]
            sims[os.path.basename(other)] = p["cos"]
    nodetect = {os.path.basename(x) for x in res["faces_not_detected"]}

    print(f"{'派生项':<28} {'ArcFace':>8}  {'门槛':>6}  判定")
    failed = []
    for f in files:
        thr, why = band_for(f)
        short = f.replace("_00001_.png", "")
        for pre in ("wang_tianwei_", "wang_kaiyu_", "fu_xingyuan_"):
            short = short.replace(pre, "")
        if thr is None:
            ok = f in nodetect
            print(f"  {short:<26} {'—':>8}  {'N/A':>6}  {'PASS' if ok else 'FAIL(不该检出人脸)'}")
            if not ok:
                failed.append(short)
            continue
        if f in nodetect:
            print(f"  {short:<26} {'未检出':>8}  {thr:>6.2f}  FAIL(应有脸却检不到)")
            failed.append(short)
            continue
        v = sims.get(f)
        ok = v is not None and v >= thr
        print(f"  {short:<26} {v:>8.4f}  {thr:>6.2f}  {'PASS' if ok else 'FAIL'}")
        if not ok:
            failed.append(short)

    print(f"\n{len(files) - len(failed)}/{len(files)} 通过分档门禁")
    if failed:
        print("需重出:", ", ".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
