#!/usr/bin/env python3
"""标定 F5-TTS 的 `speed` 参数，让中文念白落到短剧的语速区间。

## 为什么必须标定

首轮 44 条台词跑完后量语速：**中位 2.84 汉字/秒**。
参考值：中文日常对话约 4–5 字/秒，短剧快节奏念白约 5–7 字/秒。
也就是说它在用**约一半的正常语速**念台词。

这不是无关紧要的音质问题 —— animatic 的**唯一目的**就是判断叙事节奏。
用一半语速去量"台词放不放得下"，会得出"40/44 放不下、总时长超 111 秒"的结论；
按短剧常速重算则是"23/44 放不下、超 23 秒"。**两个结论会导向完全不同的改稿决定。**

慢的成因大概率是参考音频：`basic_ref_en.wav` 是英文的，F5-TTS 跨语言零样本
会连带继承参考音频的语速与韵律。正解是自制中文参考音频（并记录其文本，
`basic_ref_zh.wav` 因文本无从考据而不可用），当前用 `speed` 参数补偿。

## 为什么不直接把 speed 调到"算出来的比例"

`speed` 与实际语速不是线性的，且调过头会产生抖音明令禁止的"机械配音"。
所以逐档实测取最接近目标的那一档，而不是拿 5.5/2.84≈1.94 直接套。

用法: python pipeline/animatic/calibrate_speed.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

TTS_URL = "http://127.0.0.1:8390"
TARGET_RATE = 5.5          # 汉字/秒，短剧快节奏念白的下沿
CANDIDATES = [1.0, 1.3, 1.6, 1.9, 2.2]

# 取三条不同长度的真实台词，避免只用一句短句标定出偏的结论
SAMPLES = [
    "第一军校的集训，什么时候结束都有可能。谁完成了谁提前出来呗。",
    "睡不着，根本睡不着……",
    "不过是群孩子当面一套背后一套，把个小姑娘逼得用外壳武装自己。",
]


def post(path, payload, timeout=600):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        TTS_URL + path, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def main():
    print(f"目标语速 {TARGET_RATE} 汉字/秒（短剧快节奏念白下沿）")
    print(f"{'speed':>6} {'中位语速':>10} {'相对目标':>10}")
    results = []
    for sp in CANDIDATES:
        rates = []
        for text in SAMPLES:
            n = len(re.findall(r"[一-鿿]", text))
            try:
                r = post("/tts", {"text": text, "speed": sp})
            except Exception as e:
                print(f"{sp:>6} 请求失败 {type(e).__name__}: {e}")
                break
            d = dur(r["wav"])
            if d > 0:
                rates.append(n / d)
        if not rates:
            continue
        rates.sort()
        med = rates[len(rates) // 2]
        results.append((sp, med))
        print(f"{sp:>6} {med:>10.2f} {med / TARGET_RATE:>9.0%}")

    if not results:
        print("没有可用结果")
        return 1
    best = min(results, key=lambda x: abs(x[1] - TARGET_RATE))
    print(f"\n最接近目标的档位: speed={best[0]}（{best[1]:.2f} 字/秒）")
    print(f"用法: python pipeline/animatic/build_animatic.py --stage tts --speed {best[0]}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speed_calibration.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"target_rate_cps": TARGET_RATE,
                   "measured": [{"speed": s, "rate_cps": round(r, 3)} for s, r in results],
                   "chosen_speed": best[0],
                   "note": "语速慢的成因大概率是英文参考音频；正解是自制中文参考音频并记录其文本"},
                  f, ensure_ascii=False, indent=2)
    print(f"标定结果 -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
