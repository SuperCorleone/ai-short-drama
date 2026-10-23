#!/usr/bin/env python3
"""P1.4 · 3 分钟代理动态分镜（animatic）。

82 张代理关键帧 + F5-TTS 配音 + 时间轴 → 一条能从头看到尾的片子。
TASKS.md 称其为"本机能交付的最高价值产物"，理由是它**提前暴露叙事节奏问题** ——
而节奏问题一旦拖到 A100 终渲后才发现，返工的就不是几分钟本机算力，
而是以小时计的稀缺 A100 机时。

## 三个阶段，各自可断点续跑

    tts    每条台词生成语音，量出真实时长
    pace   台词时长 vs 镜头时长 → **节奏冲突报告**
    video  逐镜头合成片段（关键帧 + 运镜模拟 + 音轨）→ 拼接成片

## pace 阶段是重点，不是副产品

分镜脚本给每个镜头写了时长（如 `[2s]`），但那是**人估的**。
一句"精神力超出体能，会溃散死亡；体能超出精神力，身体比脑子快……"念出来要多久，
不实际合成一次没人知道。台词超出镜头时长意味着两种后果二选一：
要么加速念（机械感，撞抖音"机械配音"红线），要么拉长镜头（打乱 210 秒总时长）。
**这个冲突必须在动画分镜阶段暴露，而不是在成片阶段。**

## 为什么要模拟运镜而不是纯静帧

抖音《AI 短剧审片标准》明令禁止「静态 PPT 式敷衍内容」。虽然 animatic 是内部
review 件、不受此约束，但纯静帧看不出**运镜意图对不对**——而运镜正是这一步要锁定
的创作决策之一（架构定律 2：迭代在代理上做）。所以按 `camera_move` 字段模拟
推/拉/摇/移，让分镜脚本里写的运镜真的动起来，能被目检。

用法:
    python pipeline/animatic/build_animatic.py --stage tts
    python pipeline/animatic/build_animatic.py --stage pace
    python pipeline/animatic/build_animatic.py --stage video
    python pipeline/animatic/build_animatic.py --stage all
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(ROOT, "pipeline", "previs", "shots.json")
KF_MANIFEST = os.path.join(ROOT, "pipeline", "keyframe", "frames", "manifest.jsonl")
VOICE_DIR = os.path.join(HERE, "voice")
CLIP_DIR = os.path.join(HERE, "clips")
OUT_MP4 = os.path.join(HERE, "animatic_ep01.mp4")
PACE_REPORT = os.path.join(HERE, "pace_report.md")
TTS_URL = "http://127.0.0.1:8390"

FPS = 24
H3_DIR = None          # 由 --clips-from 设置；None = 走静帧 animatic 老路径
# 关键帧是 768x1344（9:16 竖屏代理档）。animatic 保持原生分辨率不缩放 ——
# 重采样只会引入不属于素材的模糊，而这条片子是用来看构图和节奏的。
W, H = 768, 1344

# 运镜 → 模拟方式。分镜里的运镜词五花八门，按包含关系匹配。
# zoom 值是**整段的总变化量**，按镜头时长线性摊开。
CAMERA_SIM = {
    "推": ("zoom", 0.12),      # 缓慢推近
    "拉": ("zoom", -0.12),
    "移": ("pan_x", 0.10),
    "横移": ("pan_x", 0.12),
    "摇": ("pan_x", 0.08),
    "升": ("pan_y", -0.10),
    "降": ("pan_y", 0.10),
    "手持": ("shake", 0.010),  # 轻微不规则晃动
    "固定": ("still", 0.0),
}


def post(path, payload, timeout=600):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        TTS_URL + path, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_shots():
    return json.load(open(SHOTS, encoding="utf-8"))["shots"]


def load_keyframes():
    """shot_id -> 关键帧绝对路径。只认 status=ok 的记录。"""
    m = {}
    if not os.path.exists(KF_MANIFEST):
        return m
    with open(KF_MANIFEST, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok" and rec.get("images"):
                m[rec["shot_id"]] = rec["images"][0]
    return m


def load_done(path, key="shot_id"):
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok":
                done[rec[key]] = rec
    return done


# ---------------- 阶段 1: TTS ----------------

def stage_tts(shots, speed=1.0):
    os.makedirs(VOICE_DIR, exist_ok=True)
    manifest = os.path.join(VOICE_DIR, "manifest.jsonl")
    done = load_done(manifest)

    todo = [s for s in shots
            if (s.get("speech") or {}).get("text") and s["shot_id"] not in done]
    print(f"有台词镜头 {sum(1 for s in shots if (s.get('speech') or {}).get('text'))} 个，"
          f"已完成 {len(done)}，待跑 {len(todo)}")

    with open(manifest, "a", encoding="utf-8") as log:
        for i, s in enumerate(todo, 1):
            sid = s["shot_id"]
            text = s["speech"]["text"]
            rec = {"shot_id": sid, "text": text, "speed": speed}
            t0 = time.time()
            try:
                r = post("/tts", {"text": text, "speed": speed})
                dst = os.path.join(VOICE_DIR, f"{sid}.wav")
                # 服务把 wav 落在自己的 output/ 下，搬到本阶段目录，路径自洽
                if os.path.abspath(r["wav"]) != os.path.abspath(dst):
                    with open(r["wav"], "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
                rec.update(status="ok", wav=dst, seconds=round(float(r["seconds"]), 3))
            except Exception as e:
                rec.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
            rec["elapsed"] = round(time.time() - t0, 2)
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            os.fsync(log.fileno())
            print(f"[{i}/{len(todo)}] {sid} {rec['status']} "
                  f"{rec.get('seconds', '-')}s (耗时 {rec['elapsed']}s)", flush=True)
    return 0


# ---------------- 阶段 2: 节奏冲突报告 ----------------

def wav_duration(path):
    """量 wav 的**实际时长**。

    ⚠️ 不要用 TTS 服务返回的 `seconds` —— 那个字段是**生成耗时**不是音频时长
    （`tts/server.py` 里是 `time.time() - t0`）。一度用错，把一句 7 个字的台词
    「啧……床真窄啊。」算成 **76.7 秒**（那其实是冷启加载模型的耗时），
    整份节奏报告会因此完全失真。
    本项目的纪律是**核验产物而不是信声明** —— 时长就该从文件本身量。
    """
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


HAN_RE = re.compile(r"[一-鿿]")
# 中文语速参考区间（汉字/秒）：日常对话 4–5，短剧快节奏念白 5–7
RATE_DRAMA_LO, RATE_DRAMA_HI = 5.0, 7.0
# 语速只在**足够长**的句子上算才有意义。
# 短感叹句的呼吸与首尾静音是固定开销，字数一少就被开销主导：
# 实测「关门！」2 字 2.85s = 0.70 字/秒、「薇姐——！」2 字 = 2.29 字/秒。
# 把它们计进去，全量中位被拉到 4.55（看着像"念得太慢"），
# 而真正超时的长句（≥15 字）中位是 4.65、区间 4.49–5.23，语速完全正常。
# **不设这个下限，指标会因为几句短喝而误报，进而让人去调一个根本没问题的参数。**
RATE_MIN_CHARS = 10


def stage_pace(shots):
    voices = load_done(os.path.join(VOICE_DIR, "manifest.jsonl"))
    rows, over, total_shot, total_need = [], 0, 0.0, 0.0
    rates = []

    for s in shots:
        sid = s["shot_id"]
        budget = float(s.get("duration_s") or 0)
        total_shot += budget
        v = voices.get(sid)
        spoken = wav_duration(v["wav"]) if (v and os.path.exists(v.get("wav", ""))) else 0.0
        # 留 0.3s 呼吸间隙：台词紧贴镜头边界会让剪辑点听起来被"切断"
        need = spoken + 0.3 if spoken else 0.0
        total_need += max(budget, need)
        if spoken and need > budget:
            over += 1
        chars = len(HAN_RE.findall((s.get("speech") or {}).get("text") or ""))
        rate = (chars / spoken) if (spoken > 0 and chars >= 2) else None
        if rate and chars >= RATE_MIN_CHARS:      # 见 RATE_MIN_CHARS 的说明
            rates.append(rate)
        rows.append({
            "shot_id": sid, "grade": s.get("grade"), "budget": budget,
            "spoken": round(spoken, 2), "overflow": round(need - budget, 2) if spoken else 0.0,
            "chars": chars, "rate": round(rate, 2) if rate else None,
            "speaker": (s.get("speech") or {}).get("speaker"),
            "text": ((s.get("speech") or {}).get("text") or "")[:40],
        })

    rows_over = sorted([r for r in rows if r["overflow"] > 0],
                       key=lambda r: -r["overflow"])

    med_rate = statistics.median(rates) if rates else 0.0
    rate_ok = RATE_DRAMA_LO <= med_rate <= RATE_DRAMA_HI

    lines = [
        "# animatic 节奏冲突报告（P1.4）", "",
        "## 先看语速是否可信 —— 语速不对，下面的结论全都不作数", "",
        f"- 实测中位语速：**{med_rate:.2f} 汉字/秒**"
        f"（{'✅ 落在' if rate_ok else '⚠️ 略低于'}短剧区间 {RATE_DRAMA_LO}–{RATE_DRAMA_HI}，"
        f"样本 {len(rates)} 条 ≥{RATE_MIN_CHARS} 字的台词）",
        f"- 参考：中文日常对话约 4–5 字/秒，短剧快节奏念白约 {RATE_DRAMA_LO}–{RATE_DRAMA_HI} 字/秒",
        f"- **只统计 ≥{RATE_MIN_CHARS} 字的台词**：短感叹句的呼吸与首尾静音是固定开销，"
        f"字数一少就被开销主导（实测「关门！」2 字 = 0.70 字/秒），计进去会把中位拉低成误报",
        "",
        "> **这一节必须放在最前面。** 首轮用默认 `speed=1.0` 跑出来的中位语速只有 "
        "**2.84 字/秒**（约正常的一半），",
        "> 由此得到的结论是「40/44 放不下、总时长超 111 秒」；按短剧常速重算则是"
        "「23/44 放不下、超 23 秒」。",
        "> **同一批台词，两个结论会导向完全不同的改稿决定。**",
        "> 语速慢的成因大概率是英文参考音频（F5-TTS 跨语言会继承参考音频的韵律与语速），",
        "> 当前用 `speed` 参数补偿（`speed_calibration.json`）；正解是自制中文参考音频并记录其文本。",
        "", "---", "",
        "## 节奏冲突", "",
        f"- 分镜脚本给定总时长：**{total_shot:.1f}s**",
        f"- 按实测台词时长排下来至少需要：**{total_need:.1f}s**"
        f"（超出 {total_need - total_shot:+.1f}s）",
        f"- 台词放不下的镜头：**{over} / {len(voices)}** 个有台词镜头", "",
        "> 台词时长是**从 wav 文件量出来的**，不是 TTS 服务返回的 `seconds` 字段 ——",
        "> 那个字段是生成耗时，一度把一句 7 个字的台词算成 76 秒。",
        "> 镜头时长是分镜脚本里人估的 `[Ns]`。二者冲突时只有两条路：",
        "> 再加速念（撞抖音「机械配音」红线）或拉长镜头（打乱总时长）。",
        "> **这正是 animatic 要提前暴露的东西 —— 拖到成片阶段才发现，返工的是 A100 机时。**", "",
        "## 放不下的镜头（按超出量降序）", "",
        "| 镜头 | 级别 | 给定 | 实测台词 | 超出 | 字数 | 语速 | 说话人 | 台词 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows_over:
        lines.append(f"| {r['shot_id']} | {r['grade']} | {r['budget']}s | {r['spoken']}s | "
                     f"**+{r['overflow']}s** | {r['chars']} | {r['rate']} | "
                     f"{r['speaker']} | {r['text']} |")
    if not rows_over:
        lines.append("| — | | | | | | | | 全部放得下 |")

    # 一条可执行的结论：这些镜头要么改词要么改时长，不能两样都不动
    worst = rows_over[:5]
    if worst:
        lines += ["", "## 建议", "",
                  "按超出量最大的几条看，问题不在配音而在**分镜给的时长与台词长度不匹配**：", ""]
        for r in worst:
            need_s = r["budget"]
            fit_chars = int(need_s * med_rate) if med_rate else 0
            lines.append(f"- `{r['shot_id']}` 给 {r['budget']}s，按当前语速只放得下约 "
                         f"**{fit_chars} 字**，实际写了 **{r['chars']} 字**")
        lines += ["", "两条路二选一（本报告不替创作决策）：",
                  "1. **改词**：把超长台词压到镜头时长放得下的字数",
                  "2. **改时长**：拉长这些镜头，并同步调整总时长预算"]

    with open(PACE_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"分镜总时长 {total_shot:.1f}s / 实际需要 {total_need:.1f}s "
          f"（{total_need - total_shot:+.1f}s）")
    print(f"台词放不下的镜头 {over}/{len(voices)} → {PACE_REPORT}")
    for r in rows_over[:10]:
        print(f"  {r['shot_id']}  给 {r['budget']}s 实需 {r['spoken']}s  超 +{r['overflow']}s")
    return 0


# ---------------- 阶段 3: 逐镜头合成 ----------------

def camera_filter(move, dur):
    """把 camera_move 翻成 ffmpeg filter。返回 filter 串。

    用 zoompan 的 `on`（输出帧序号）线性表达运动量，而不是让 zoom 自增 ——
    自增式（`z='min(zoom+0.001,1.2)'`）的总位移取决于帧数，镜头一长就冲顶，
    短镜头又几乎不动，两头都不对。按 on/总帧数 归一化才能让每个镜头
    无论长短都走完同样的运镜幅度。
    """
    frames = max(2, int(round(dur * FPS)))
    kind, amt = "still", 0.0
    for key, (k, a) in CAMERA_SIM.items():
        if move and key in move:
            kind, amt = k, a
            break

    if kind == "zoom":
        # 推：1 → 1+amt；拉：1+|amt| → 1（起点先放大，再缩回去）
        # 别写成靠字符串拼接凑负号的形式（`f"{1-amt}{amt}*on/..."`）——
        # 那种写法碰巧能生成 "1.12-0.12*on/24"，但读的人得先在脑子里跑一遍才知道对不对
        start = 1.0 if amt > 0 else 1.0 + abs(amt)
        delta = amt if amt > 0 else -abs(amt)
        z = f"{start:.4f}{delta:+.4f}*on/{frames}"
        expr = f"z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif kind == "pan_x":
        # 先放大留出摇移余量，再横向移动裁切窗
        expr = (f"z='1.12':x='(iw-iw/zoom)*({amt / 0.12})*on/{frames}'"
                f":y='ih/2-(ih/zoom/2)'")
    elif kind == "pan_y":
        expr = (f"z='1.12':x='iw/2-(iw/zoom/2)'"
                f":y='(ih-ih/zoom)*({abs(amt) / 0.10})*on/{frames}'")
    elif kind == "shake":
        # 手持：两个不同周期的正弦叠加，避免看出规律性
        expr = (f"z='1.06':x='iw/2-(iw/zoom/2)+{amt}*iw*sin(on/7)'"
                f":y='ih/2-(ih/zoom/2)+{amt}*ih*sin(on/11)'")
    else:
        expr = "z='1.02':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"

    return f"zoompan={expr}:d=1:s={W}x{H}:fps={FPS}"


def build_clip(shot, img, wav, dst):
    dur = float(shot.get("duration_s") or 2.0)
    # 输入既可以是静帧（animatic：靠 camera_filter 模拟运镜），
    # 也可以是 H3 生成的真视频（整集成片：运镜已经在画面里，**不能再叠滤镜**，
    # 否则等于在真运动上再推一层假运动）。按扩展名分流，其余逻辑（按分镜时长裁剪、
    # apad 补静音防音画漂移、统一编码参数供 concat 流拷贝）两条路径完全共用。
    is_video = img.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
    if is_video:
        # H3 统一出 5.17s，而分镜多为 1–3s ⇒ 从头截取 dur。
        # 截头不截尾：镜头的叙事重点在前段，且 H3 尾段更容易出现动作走形
        vf = f"fps={FPS}"
        cmd = ["ffmpeg", "-y", "-t", f"{dur}", "-i", img]
    else:
        vf = camera_filter(shot.get("camera_move"), dur)
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{dur}", "-i", img]
    if wav:
        cmd += ["-i", wav]
    else:
        cmd += ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=24000:cl=mono"]
    cmd += [
        "-vf", vf,
        # 台词比镜头**长**时截断音轨而不是拉长画面：animatic 要如实反映
        # "按分镜时长剪会发生什么"，自动补时长等于把问题藏起来（pace 报告已单列）。
        # 台词比镜头**短**时用 apad 补静音到等长 —— 否则该段音轨早于画面结束，
        # 82 段用 concat demuxer 拼接时这些缺口会**累积成音画漂移**。
        "-af", "apad",
        "-t", f"{dur}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "24000", "-ac", "1",
        "-r", str(FPS), dst,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])


def load_h3_clips(d):
    """H3 整集产物：`<shot_id>_<tag>_00001_.mp4`。缺片直接不进 map，由上层报 warn。"""
    import glob as _g
    out = {}
    for p in _g.glob(os.path.join(d, "*.mp4")):
        sid = os.path.basename(p).split("_")[0]
        if sid.startswith("G") and "-S" in sid:
            out[sid] = p
    return out


def stage_video(shots):
    os.makedirs(CLIP_DIR, exist_ok=True)
    kfs = load_h3_clips(H3_DIR) if H3_DIR else load_keyframes()
    voices = load_done(os.path.join(VOICE_DIR, "manifest.jsonl"))
    manifest = os.path.join(CLIP_DIR, "manifest.jsonl")
    done = load_done(manifest)

    missing = [s["shot_id"] for s in shots if s["shot_id"] not in kfs]
    if missing:
        print(f"[warn] {len(missing)} 个镜头没有关键帧，将被跳过: {missing[:8]}")

    todo = [s for s in shots if s["shot_id"] in kfs and s["shot_id"] not in done]
    print(f"待合成 {len(todo)} 个片段（已完成 {len(done)}）")

    with open(manifest, "a", encoding="utf-8") as log:
        for i, s in enumerate(todo, 1):
            sid = s["shot_id"]
            dst = os.path.join(CLIP_DIR, f"{sid}.mp4")
            rec = {"shot_id": sid, "duration_s": s.get("duration_s"),
                   "camera_move": s.get("camera_move")}
            try:
                v = voices.get(sid)
                build_clip(s, kfs[sid], v["wav"] if v else None, dst)
                rec.update(status="ok", clip=dst)
            except Exception as e:
                rec.update(status="failed", error=str(e)[:300])
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            os.fsync(log.fileno())
            print(f"[{i}/{len(todo)}] {sid} {rec['status']}", flush=True)

    # 拼接。用 concat demuxer 而不是 filter_complex：片段已统一编码参数，
    # demuxer 走流拷贝，82 段几秒钟完事，也不会二次损失画质。
    clips = load_done(manifest)
    ordered = [clips[s["shot_id"]]["clip"] for s in shots if s["shot_id"] in clips]
    if not ordered:
        print("[error] 没有可拼接的片段")
        return 1
    lst = os.path.join(CLIP_DIR, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for c in ordered:
            f.write(f"file '{c.replace(chr(92), '/')}'\n")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", OUT_MP4],
        capture_output=True, text=True, timeout=1200)
    if r.returncode != 0:
        print(f"[error] 拼接失败: {r.stderr[-500:]}")
        return 1

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", OUT_MP4],
        capture_output=True, text=True, timeout=120)
    print(f"\n成片 {OUT_MP4}")
    print(f"片段 {len(ordered)}/{len(shots)}   时长 {probe.stdout.strip()}s")
    return 0


def _self_test():
    """用真实关键帧验证运镜 filter。

    zoompan 的表达式是**运行时**才由 ffmpeg 解析的：写错了不会在 Python 层报错，
    只会让 ffmpeg 报一句语法错，或者更糟 —— 静默生成一段不动的片子。
    所以必须真跑一遍并核验产物（时长、帧数、以及画面**确实在动**）。
    """
    kfs = load_keyframes()
    if not kfs:
        print("[skip] 还没有关键帧，先跑 run_zimage.py")
        return 1
    sid, img = sorted(kfs.items())[0]
    tmp = os.path.join(CLIP_DIR, "_selftest")
    os.makedirs(tmp, exist_ok=True)
    cases = []

    for move, label in [("固定", "still"), ("缓推", "zoom_in"), ("拉", "zoom_out"),
                        ("横移", "pan_x"), ("手持", "shake")]:
        dst = os.path.join(tmp, f"{label}.mp4")
        shot = {"duration_s": 2.0, "camera_move": move}
        try:
            build_clip(shot, img, None, dst)
            ok_exists = os.path.exists(dst) and os.path.getsize(dst) > 0
        except Exception as e:
            cases.append((f"{label}({move}) 合成", False, str(e)[:200]))
            continue

        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_frames", "-show_entries", "stream=nb_read_frames,duration",
             "-of", "json", dst], capture_output=True, text=True, timeout=180)
        info = json.loads(p.stdout)["streams"][0]
        frames = int(info.get("nb_read_frames", 0))
        cases.append((f"{label}({move}) 合成", ok_exists, ""))
        cases.append((f"{label} 帧数≈{FPS}×2s", abs(frames - FPS * 2) <= 2, f"实得 {frames}"))

        # 画面真的在动吗？取第 0 帧与最后一帧比像素差。
        # 只看 ffmpeg 退出码是不够的 —— 表达式恒等于常数时它照样成功，出一段死片。
        def frame_bytes(t):
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(t), "-i", dst, "-frames:v", "1",
                 "-vf", "scale=64:112", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                capture_output=True, timeout=120)
            return r.stdout

        a, b = frame_bytes(0.05), frame_bytes(1.9)
        if a and b and len(a) == len(b):
            diff = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
        else:
            diff = -1
        moving = diff > 0.5
        cases.append((f"{label} 画面{'应在动' if move != '固定' else '几乎不动'}",
                      moving if move != "固定" else True, f"首末帧平均像素差 {diff:.2f}"))

    ok = sum(1 for _, c, _ in cases if c)
    for name, cond, detail in cases:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    print(f"\n运镜自检 {ok}/{len(cases)} 通过（样本镜头 {sid}）")
    return 0 if ok == len(cases) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-from", default="",
                    help="用该目录下的 H3 视频替代静帧关键帧，产出整集成片而非 animatic")
    ap.add_argument("--stage", choices=["tts", "pace", "video", "all"], default="all")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.clips_from:
        # 整集成片走独立的产出目录与文件名：**绝不覆盖 animatic 的产物**，
        # 两者是不同东西（一个是代理预演，一个是成片），混在一起没法对比
        global H3_DIR, CLIP_DIR, OUT_MP4
        H3_DIR = os.path.abspath(args.clips_from)
        CLIP_DIR = os.path.join(HERE, "clips_ep01")
        OUT_MP4 = os.path.join(ROOT, "pipeline", "exports", "ep01_h3.mp4")
        os.makedirs(os.path.dirname(OUT_MP4), exist_ok=True)

    if args.self_test:
        return _self_test()

    shots = load_shots()
    rc = 0
    if args.stage in ("tts", "all"):
        rc |= stage_tts(shots, args.speed)
    if args.stage in ("pace", "all"):
        rc |= stage_pace(shots)
    if args.stage in ("video", "all"):
        rc |= stage_video(shots)
    return rc


if __name__ == "__main__":
    sys.exit(main())
