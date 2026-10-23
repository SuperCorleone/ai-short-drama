"""P1.5 · 流水线门禁（L0 规格合规 + L1 帧级指标）。

判定三态，对应 TASKS.md「回滚颗粒度 = 镜头」的纪律：
    PASS   —— 全部通过，进入下一层
    RETRY  —— 质量未达标但可救，按 grade 给重抽预算（S 3 次 / B 2 次 / C 不重抽）
    REJECT —— 硬性规格或合规不达标，重抽也没用，必须改上游

为什么 L0 和 L1 分开：
  L0 是**硬性**的（分辨率/帧率/码率/响度/时长/AI 标识），不达标是编码或装配的错，
     重抽多少次都不会变好 → 直接 REJECT；
  L1 是**质量**的（构图符合度/一致性/身份/运动/画质），换个种子可能就过了 → RETRY。
把两者混在一个阈值表里，会让"忘了加 AI 标识"这种事被当成"再抽一次试试"。

自检：内置 self-test，用构造样本验证每条阈值的边界行为
（刚好达标=PASS、差一点=RETRY/REJECT）。跑不过就是门禁本身写错了。

用法:
    eval/venv/Scripts/python.exe eval/gate.py --self-test
    eval/venv/Scripts/python.exe eval/gate.py --video path.mp4 --prompt "..." --grade S
    eval/venv/Scripts/python.exe eval/gate.py --image path.png --prompt "..." --json
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

EVAL = "http://127.0.0.1:8391"

PASS, RETRY, REJECT = "PASS", "RETRY", "REJECT"

# --- L1 质量阈值（依据见 ARCHITECTURE.md §4.1 与 TASKS.md 门禁阈值表）---
L1 = {
    # 名称: (取值键, 下限, 上限, 说明)
    # clip_t 阈值 2026-08-08 重新标定：原 0.30 是按**英文** CLIP(ViT-B-32/laion2b)
    # 的分数尺度定的，而该模型对中文毫无鉴别力（切题 0.1933 < 不相关 0.2017），
    # 已换多语言 xlm-roberta-base-ViT-B-32/laion5b。新尺度下实测三点：
    #     好图+切题中文 0.2582 / 好图+不相关中文 0.027 / 全黑图+切题中文 0.1309
    # 暂定 0.20（落在"全黑 0.13"与"好图 0.26"之间）。
    # ⚠️ 这是**三个样本的临时值**，不是标定完成的阈值 —— 正式标定应在 RQ5
    # （自动分 vs 人工终审的秩相关）里用足量样本回归确定。
    "clip_t":    ("clip_t", 0.20, None, "构图/提示词符合度（多语言 CLIP，阈值待 RQ5 正式标定）"),
    "dinov2":    ("dinov2", 0.90, None, "时序一致性（M0 实测 0.9603）"),
    "arcface":   ("arcface", 0.75, None, "角色身份保持 vs 角色圣经基准脸"),
    # 下界防抖音明令禁止的「静态 PPT 式敷衍内容」，上界防运动过大糊成一团
    "raft_flow": ("raft_flow", 0.20, 2.00, "运动合理性"),
    "musiq":     ("musiq", 55.0, None, "成像质量（M0 基线 50.16 是 1.3B 草稿档）"),
}

# --- L0 硬性交付规格 ---
L0 = {
    "width": 1080, "height": 1920,      # 竖屏，红果/抖音信息流
    "min_fps": 24.0,
    "min_bitrate_kbps": 8000,
    "target_lufs": -14.0, "lufs_tol": 2.0,   # EBU R128
    "min_duration_s": 30.0,             # 抖音明文：单集建议 ≥30 秒
}

RETRY_BUDGET = {"S": 3, "B": 2, "C": 0}


def post(path, payload, timeout=900):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        EVAL + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def read_ailabel_sidecar(video_path):
    """读 `pipeline/deliver/inject_ai_label.py` 落的凭据，返回 True/False/None。

    `--ai-label` 原本是人手拨的开关。在一条要跑几十集的流水线上，人手拨的开关
    迟早会拨错，而且错的方向必然是"标成已加"——因为只有那样门禁才放行。
    这里改成读注入脚本落下的**事实**：三处产物实际存在才算数。
    None 表示没有凭据（既不判过也不判不过），保持原有的"未提供则跳过"语义。
    """
    side = video_path + ".ailabel.json"
    if not os.path.exists(side):
        return None
    try:
        rec = json.load(open(side, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # 不轻信 all_three_present 这个字段本身，回查三个文件是否真在
    for key in ("video", "cover", "desc"):
        p = rec.get(key)
        if not p or not os.path.exists(p):
            return False
    return True


def ffprobe(path):
    """取交付规格。ffprobe 拿不到的（响度）单独用 ffmpeg loudnorm 量。"""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {out.stderr[:300]}")
    d = json.loads(out.stdout)
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)
    fps = 0.0
    if v and v.get("r_frame_rate", "0/1") != "0/0":
        num, den = (v["r_frame_rate"].split("/") + ["1"])[:2]
        fps = float(num) / float(den or 1)
    return {
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": round(fps, 3),
        "vcodec": v.get("codec_name") if v else None,
        "has_audio": a is not None,
        "duration_s": round(float(d["format"].get("duration", 0) or 0), 2),
        "bitrate_kbps": round(float(d["format"].get("bit_rate", 0) or 0) / 1000, 1),
    }


def measure_lufs(path, timeout=600):
    """用 ffmpeg loudnorm 的分析通道量整体响度（EBU R128）。"""
    cmd = ["ffmpeg", "-nostats", "-hide_banner", "-i", path,
           "-af", "loudnorm=print_format=json", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    txt = out.stderr or ""
    start = txt.rfind("{")
    end = txt.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return float(json.loads(txt[start:end + 1]).get("input_i"))
    except Exception:
        return None


def check_l0(spec, lufs, ai_label):
    """硬性规格。返回 (通过?, 违规明细)。"""
    bad = []
    if (spec["width"], spec["height"]) != (L0["width"], L0["height"]):
        bad.append(f"分辨率 {spec['width']}x{spec['height']} != {L0['width']}x{L0['height']}（竖屏交付）")
    if spec["fps"] < L0["min_fps"]:
        bad.append(f"帧率 {spec['fps']} < {L0['min_fps']}")
    if spec["bitrate_kbps"] and spec["bitrate_kbps"] < L0["min_bitrate_kbps"]:
        bad.append(f"码率 {spec['bitrate_kbps']}kbps < {L0['min_bitrate_kbps']}")
    if spec["duration_s"] < L0["min_duration_s"]:
        bad.append(f"时长 {spec['duration_s']}s < {L0['min_duration_s']}s（抖音单集下限）")
    if lufs is not None and abs(lufs - L0["target_lufs"]) > L0["lufs_tol"]:
        bad.append(f"响度 {lufs:.1f} LUFS 偏离 {L0['target_lufs']}±{L0['lufs_tol']}")
    if ai_label is False:
        bad.append("缺 AI 标识（片头前3s + 封面 + 简介 三处，国家《标识办法》强制）")
    return (not bad), bad


def check_l1(metrics):
    """质量指标。返回 (通过?, 未达标明细)。缺项（如未给参考脸）跳过，不算失败。"""
    bad = []
    for name, (key, lo, hi, why) in L1.items():
        v = metrics.get(key)
        if v is None:
            continue
        if lo is not None and v < lo:
            bad.append(f"{name}={v} < {lo}（{why}）")
        if hi is not None and v > hi:
            bad.append(f"{name}={v} > {hi}（{why}）")
    return (not bad), bad


def decide(l0_ok, l0_bad, l1_ok, l1_bad, grade, attempt):
    """三态判定。L0 不过一律 REJECT——重抽改不了编码/装配问题。"""
    if not l0_ok:
        return REJECT, l0_bad
    if l1_ok:
        return PASS, []
    budget = RETRY_BUDGET.get(grade, 2)
    if attempt >= budget:
        return REJECT, l1_bad + [f"已用尽 {grade} 级重抽预算（{budget} 次）"]
    return RETRY, l1_bad


# ---------------------------------------------------------------- self-test
def _self_test():
    """构造样本验证每条阈值的边界行为。跑不过说明门禁本身写错了。"""
    good_spec = {"width": 1080, "height": 1920, "fps": 24.0, "vcodec": "h264",
                 "has_audio": True, "duration_s": 210.0, "bitrate_kbps": 9000.0}
    # 边界值**从 L1 表推导**，不硬编码：否则每次调阈值自检就漂
    # （2026-08-08 把 clip_t 从 0.30 改到 0.20 时，硬编码版立刻假失败一条）。
    good_m = {key: lo for _n, (key, lo, _hi, _w) in L1.items() if lo is not None}
    # 各项"差一点"的步长：按量纲取，musiq 是 0-100 分制故用 1.0，其余用 0.01
    deltas = {k: (-1.0 if k == "musiq" else -0.01) for k in good_m}
    cases = []

    # 1) 每项刚好压线 → 必须 PASS（阈值是"含等号"的下限）
    ok, bad = check_l0(good_spec, -14.0, True)
    cases.append(("L0 全部压线 → 通过", ok is True, bad))
    ok, bad = check_l1(good_m)
    cases.append(("L1 全部压线 → 通过", ok is True, bad))

    # 2) L1 每项各差一点 → 必须逐项抓出
    for key, delta in deltas.items():
        m = dict(good_m); m[key] = round(m[key] + delta, 4)
        ok, bad = check_l1(m)
        cases.append((f"L1 {key} 低于下限 → 抓出", ok is False and len(bad) == 1, bad))

    # 3) 运动上界（防糊）
    hi = L1["raft_flow"][2]
    m = dict(good_m); m["raft_flow"] = round(hi + 0.01, 4)
    ok, bad = check_l1(m)
    cases.append(("L1 raft_flow 超上界 → 抓出", ok is False and len(bad) == 1, bad))

    # 4) 缺项不算失败（未给参考脸时 arcface 为 None，是正确行为）
    m = dict(good_m); m["arcface"] = None
    ok, bad = check_l1(m)
    cases.append(("L1 arcface 缺项 → 跳过不判失败", ok is True, bad))

    # 5) L0 每项各违规一次
    for tweak, label in [
        ({"width": 1920, "height": 1080}, "横屏"),
        ({"fps": 23.0}, "帧率不足"),
        ({"bitrate_kbps": 5000.0}, "码率不足"),
        ({"duration_s": 29.0}, "时长不足"),
    ]:
        sp = dict(good_spec); sp.update(tweak)
        ok, bad = check_l0(sp, -14.0, True)
        cases.append((f"L0 {label} → 抓出", ok is False and len(bad) == 1, bad))
    ok, bad = check_l0(good_spec, -18.0, True)
    cases.append(("L0 响度偏离 → 抓出", ok is False and len(bad) == 1, bad))
    ok, bad = check_l0(good_spec, -14.0, False)
    cases.append(("L0 缺 AI 标识 → 抓出", ok is False and len(bad) == 1, bad))

    # 6) 三态判定：L0 不过必须 REJECT，不能给重抽机会
    d, _ = decide(False, ["x"], True, [], "S", 0)
    cases.append(("L0 不过 → REJECT（不给重抽）", d == REJECT, d))
    d, _ = decide(True, [], False, ["y"], "S", 0)
    cases.append(("L1 不过且有预算 → RETRY", d == RETRY, d))
    d, _ = decide(True, [], False, ["y"], "S", 3)
    cases.append(("S 级用尽 3 次 → REJECT", d == REJECT, d))
    d, _ = decide(True, [], False, ["y"], "B", 2)
    cases.append(("B 级用尽 2 次 → REJECT", d == REJECT, d))
    d, _ = decide(True, [], False, ["y"], "C", 0)
    cases.append(("C 级无重抽预算 → REJECT", d == REJECT, d))
    d, _ = decide(True, [], True, [], "C", 0)
    cases.append(("全通过 → PASS", d == PASS, d))

    fails = 0
    for name, ok, detail in cases:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print(f"         detail={detail}")
            fails += 1
    print(f"\n自检 {len(cases) - fails}/{len(cases)} 通过")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--video")
    ap.add_argument("--image")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--ref-face")
    ap.add_argument("--grade", default="B", choices=["S", "B", "C"])
    ap.add_argument("--attempt", type=int, default=0, help="已重抽次数")
    ap.add_argument("--ai-label", dest="ai_label", action="store_true", default=None)
    ap.add_argument("--no-ai-label", dest="ai_label", action="store_false")
    ap.add_argument("--skip-l0", action="store_true", help="镜头级中间产物不查交付规格")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.video and not args.image:
        ap.error("需要 --video 或 --image（或 --self-test）")

    out = {"target": args.video or args.image, "grade": args.grade, "attempt": args.attempt}

    if args.video:
        metrics = post("/eval", {"video": args.video, "prompt": args.prompt,
                                 "ref_face": args.ref_face, "max_frames": 16})
        if args.skip_l0:
            l0_ok, l0_bad = True, []
        else:
            spec = ffprobe(args.video)
            lufs = measure_lufs(args.video) if spec["has_audio"] else None
            out["spec"] = spec
            out["lufs"] = lufs
            ai_label = args.ai_label
            if ai_label is None:
                ai_label = read_ailabel_sidecar(args.video)
                out["ai_label_source"] = "sidecar" if ai_label is not None else "未提供"
            else:
                out["ai_label_source"] = "命令行"
            l0_ok, l0_bad = check_l0(spec, lufs, ai_label)
    else:
        metrics = post("/eval_image", {"image": args.image, "prompt": args.prompt,
                                       "ref_face": args.ref_face})
        l0_ok, l0_bad = True, []   # 单帧没有交付规格可言

    out["metrics"] = metrics
    l1_ok, l1_bad = check_l1(metrics)
    verdict, reasons = decide(l0_ok, l0_bad, l1_ok, l1_bad, args.grade, args.attempt)
    out["verdict"] = verdict
    out["reasons"] = reasons

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"判定: {verdict}  ({args.grade} 级，已重抽 {args.attempt} 次)")
        for k, v in metrics.items():
            if k not in ("seconds", "frames_used"):
                print(f"  {k:12s} {v}")
        for r in reasons:
            print(f"  ✗ {r}".replace("✗", "!"))
    return {PASS: 0, RETRY: 1, REJECT: 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
