#!/usr/bin/env python3
"""P1.0b · 为无画面描述的镜头回填 `action`（L0 层 Qwen3-32B 到位前的兜底）。

## 背景

`parse_shotlist.py` 修好台词错分后暴露出内容缺口：**32/82 镜头的 `action` 为空**
—— 原文对这些镜头只写了台词，没写画面。它们的 L3 图像提示词无从生成
（提示词 = scene + action + shot_size + camera_move，action 空则只剩景别和运镜）。

正解是 L0 层用 Qwen3-32B 结合上下文补全，但 A100 在排队（78351）。本脚本是**兜底**，
不是替代：产出全部带 `action_source="derived"` 与置信度，L0 到位后应整体覆盖。

## 三条设计原则

1. **不重复 scene**。下游提示词已经是 `scene + action + shot_size + camera_move`
   （见 `a100/run_h3_ref2va.py:138`），action 里再写一遍环境等于给扩散模型喂重复词。
   所以 action 只描述**主体与表演**。

2. **区分「配音指示」与「表演指示」**。`speech.emotion` 形如「疲惫沙哑，慢」——
   「沙哑」「慢」是给 TTS 的音质与语速，**不是画面信息**；喂进扩散模型是纯噪声。
   只有「疲惫」该进图像提示词。两者已经分流到不同的消费端，这里必须切干净。

3. **画面主体 ≠ `characters` 字段**。`extract_characters()` 吃的是整行文本（含台词），
   且无信号时回落到组资产标签，所以它的真实语义是"这句话提到了谁"：
   - `G03-S02` 的 `['丧尸']` 来自 OS 台词"被一只五级丧尸舔了"
   - `G04-S06` 的 `['骆驼']` 是组资产兜底，画面里根本没有骆驼
   拿它当画面主体会直接渲错人。故另立 `subject` 字段，**不动 `characters` 的语义**
   （`build_previs.mjs` 等在消费它，改语义会静默影响预演管线）。

## 置信度分级（低置信项必须能被一眼挑出来复核）

| 级别 | 依据 | 数量 |
|---|---|---|
| high | 原文括号明写主体，如 `近景（王开宇）`、`特写（镜中：脸→指甲）` | 14 |
| medium | 说话人即主体（`voice_type=character` 时，这类镜头就是在拍他说话） | 5 |
| low | 纯上下文推断（OS 独白，靠场景与相邻镜头猜） | 13 |

`low` 的那些是**已知的弱项**，不假装它们可靠 —— 报告里单列，L0 到位后优先覆盖。

用法:
    python scripts/backfill_action.py --dry-run     # 只看，不写
    python scripts/backfill_action.py               # 写回 shots.json（自动备份）
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "pipeline" / "previs" / "shots.json"
REPORT = ROOT / "pipeline" / "previs" / "action_backfill_report.md"

PROTAGONIST = "王天薇"     # OS 内心独白的固定归属（穿越者视角，全片第一人称）

# ── 配音指示词：出现在 emotion 里但**不该进图像提示词** ────────────────
# 分两类：音质（沙哑/气声/破音…）与语速节奏（慢/渐快/拖长/一顿…）。
# 它们的正确去处是 TTS 的 emotion 参数，那条路径已经有了（speech.emotion 原样保留）。
# ⚠️ 只放**多字**音质词，且只在段内做子串剥离。
# 单字的「快」「慢」绝不能进这个表：`"依旧欢快".replace("快","")` → `"依旧欢"`，
# 首次干跑就踩到了。单字词只能靠 PURE_VOCAL_SEGMENTS 的**整段相等**判定丢弃。
VOCAL_TOKENS = [
    "压成气音", "语速快", "快而轻", "一字一句", "渐快", "渐慢",
    "放缓", "放慢", "放轻", "中速", "沙哑", "气声", "破音", "拖长", "短促", "咬字",
]
# 整段都是配音/节奏指示 → 整段丢弃（含单字词，只做等值匹配）
PURE_VOCAL_SEGMENTS = {
    "慢", "快", "渐快", "渐慢", "放缓", "放慢", "放轻", "中速", "低声", "轻声",
    "气声", "破音", "拖长", "短促", "一字一句", "快而轻", "语速快", "沙哑",
    "接上", "一顿", "稳住",
}
# 剥掉音质词后剩下的**非视觉残渣**：既不是表情也不是动作，进提示词只会是噪声。
# 例：「下意识压成气音」剥完剩「下意识」；「给出判词」「找回节奏」是叙事描述不是画面。
NON_VISUAL_RESIDUE = {"下意识", "给出判词", "找回节奏", "接上", "一顿", "细数"}

# ── 原文括号里的非人名主体：只有 5 种，逐条手写比通用规则准 ──────────────
# 通用化没有意义（总共 82 个镜头，且这是 L0 到位前的临时件）；
# 手写的代价是不可泛化，收益是每一条都对得上原文语义。未命中的走 verbatim 兜底。
#
# 第二个值 = **这个取景里能不能看见脸**。它决定要不要写表情与「嘴唇微闭」——
# 「手+食材」的特写里根本没有脸，再写「嘴唇微闭、沉默不语」是给扩散模型
# 一个它无法满足、只会引发构图错乱的要求（可能硬塞一张脸进来）。
HINT_FRAMING = {
    "屏幕+侧脸": ("悬浮光屏与她的侧脸同框，屏光映在脸上", True),
    "手+食材": ("她的双手与摊开的食材占满画面", False),
    "镜中：脸→指甲": ("全身镜里映出她的脸，视线下移到涂得斑驳的指甲", True),
    "镜中": ("全身镜里映出她的身影", True),
    "真人+镜中同框": ("她本人与镜中倒影同框", True),
}

SPEAK_STATE = {
    "os":        "嘴唇微闭、沉默不语",        # OS = 画外内心独白，人物不出声
    "character": "正在说话",
    "robot":     "正在应答",
    "broadcast": "画面无人开口",
    "system":    "画面无人开口",
    "sfx":       "",
}


def clean_emotion(emotion):
    """剥掉配音指示，只留表演指示。返回可直接进图像提示词的短语（可能为空）。"""
    if not emotion:
        return ""
    segs = [s.strip() for s in re.split(r"[，,、]", emotion) if s.strip()]
    kept = []
    for seg in segs:
        if seg in PURE_VOCAL_SEGMENTS:
            continue
        for t in VOCAL_TOKENS:                 # 段内混合，如「疲惫沙哑」→「疲惫」
            seg = seg.replace(t, "")
        seg = seg.strip()
        if seg and seg not in NON_VISUAL_RESIDUE:
            kept.append(seg)
    return "、".join(kept)


def pov_content(shot):
    """主观视角镜头看到的是**她眼里的东西**，不是她的脸。

    `perspective_raw` 的括号里就写着答案（`主观（舱顶视角）`），和 `subject_hint`
    属于同一类"原文明写但容易漏读"的信息。漏掉它会把一个第一人称仰视镜头
    渲成人物面部特写 —— 景别对了、内容全错。
    """
    praw = shot.get("perspective_raw") or ""
    if shot.get("perspective") != "主观":
        return None
    m = re.search(r"[（(]([^）)]*)[）)]", praw)
    return (m.group(1).strip() if m else "") or None


def resolve_subject(shot, prev_subject):
    """决定画面主体。返回 (subject, framing, face_visible, confidence, why)。

    优先级从强到弱，每一级都记下依据 —— 事后复核时能直接看出这条是怎么来的，
    而不是面对一句无从追溯的生成文本。
    """
    hint = (shot.get("subject_hint") or "").strip()
    sp = shot.get("speech") or {}
    speaker = (sp.get("speaker") or "").strip()
    vtype = sp.get("voice_type")

    # ⓪ 主观视角优先于一切主体判定：画面里根本没有"主体人物"，只有她看到的东西。
    # 例外是括号已给出取景（如「手+食材」本身就是 POV 内容），交给 ① 处理。
    pov = pov_content(shot)
    if pov and not hint:
        # 前缀必须与 pipeline/keyframe/l3_prompt.py 的 POV_MARKER 一致 ——
        # 下游靠它判断"该不该描述主角的长相"（主观视角里她在镜头后面）
        return (PROTAGONIST, f"第一人称主观视角，画面是{pov}", False,
                "high", f"原文视角括号「主观（{pov}）」")

    # ① 原文括号明写
    if hint:
        if hint in HINT_FRAMING:
            text, face = HINT_FRAMING[hint]
            return PROTAGONIST, text, face, "high", f"原文括号「{hint}」"
        if re.fullmatch(r"[一-龥]{2,4}", hint) and hint not in ("镜中像", "过肩双人"):
            return hint, "", True, "high", f"原文括号指明主体「{hint}」"
        return PROTAGONIST, hint, True, "high", f"原文括号「{hint}」（原样用作取景描述）"

    # ② 说话人即主体：这类镜头存在的理由就是拍他说这句话
    if vtype in ("character", "robot") and speaker and speaker != "OS":
        return speaker, "", True, "medium", f"说话人「{speaker}」即画面主体"

    # ③ OS 内心独白 → 归属主角；但闪回插入镜（场景切到回忆地点）主体另判
    if vtype == "os":
        scene = shot.get("scene")
        chars = [c for c in (shot.get("characters") or []) if c != PROTAGONIST]
        if scene in ("雪夜基地", "浴缸五金店") and chars:
            # 闪回镜头：characters 此时**可能**指向回忆里的对象，但它同样可能是
            # 组资产兜底的产物（G04-S06 的「骆驼」就是）。故置信度压到 low。
            return (chars[0], "", True, "low",
                    f"闪回镜（场景={scene}），取 characters[0]（可能是资产兜底，需复核）")
        return PROTAGONIST, "", True, "low", "OS 内心独白，归属主角"

    # ④ 延续上一镜主体
    if prev_subject:
        return prev_subject, "", True, "low", f"延续上一镜主体「{prev_subject}」"
    return PROTAGONIST, "", True, "low", "无信号，回落到主角"


def compose(shot, prev_subject):
    subject, framing, face_visible, conf, why = resolve_subject(shot, prev_subject)
    sp = shot.get("speech") or {}
    vtype = sp.get("voice_type") or ""
    expr = clean_emotion(sp.get("emotion"))
    speak = SPEAK_STATE.get(vtype, "")

    # OS 是**旁白者**（主角）的情绪。闪回插入镜里画面主体不是主角时，把旁白的
    # 情绪安到对方脸上是错的 —— 首次干跑产出过「丧尸，咬牙切齿」，
    # 而「咬牙切齿」是王天薇讲述这段往事时的语气。
    # 「嘴唇微闭、沉默不语」同理是旁白者的状态描述，套到丧尸头上没有意义
    if vtype == "os" and subject != PROTAGONIST:
        expr = ""
        speak = ""

    # 脸不在画面里（POV、手部特写）就别描述表情与口型 —— 那是模型无法满足的要求，
    # 强行给会诱导它硬塞一张脸进来，把一个手部特写变成人像。
    if not face_visible:
        expr = ""
        speak = ""

    # 不加「神情」前缀：清洗后的词既有表情（皱眉/愣住）也有动作（看了眼时间/头也不回），
    # 统一冠以「神情」会产出「神情看了眼时间」这种病句。作独立短语反而两类都通顺。
    parts = [framing] if framing else [subject]
    if expr:
        parts.append(expr)
    if speak:
        parts.append(speak)
    return "，".join(p for p in parts if p), subject, conf, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--shots", default=str(SHOTS))
    args = ap.parse_args()

    path = Path(args.shots)
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = data["shots"]

    filled, rows = 0, []
    prev_subject = None
    for s in shots:
        # **可重跑**：已经是 derived 的重新推导（规则改了就该重新生成），
        # original 的一个字都不动。不这样做的话，改一条规则就得手工回滚 shots.json。
        is_derived = s.get("action_source") == "derived"
        has_original_action = bool((s.get("action") or "").strip()) and not is_derived
        if has_original_action:
            subj, _, _, _, _ = resolve_subject(s, prev_subject)
            s["subject"] = subj
            s["action_source"] = "original"
            prev_subject = subj
            continue

        action, subj, conf, why = compose(s, prev_subject)
        s["action"] = action
        s["subject"] = subj
        s["action_source"] = "derived"
        s["action_confidence"] = conf
        s["action_derivation"] = why
        prev_subject = subj
        filled += 1
        rows.append((s["shot_id"], conf, subj, action, why))

    order = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (order[r[1]], r[0]))

    print(f"回填 {filled} 个镜头的 action")
    from collections import Counter
    print("置信度分布:", dict(Counter(r[1] for r in rows)))
    print("-" * 100)
    for sid, conf, subj, action, why in rows:
        print(f"{sid:9s} [{conf:6s}] {subj:6s} {action}")
        print(f"{'':9s}          └ {why}")

    if args.dry_run:
        print("\n--dry-run：未写入")
        return 0

    # 原始基线只留一份：本脚本可重跑，若每次都覆盖备份，第二次跑完就再也拿不到
    # "回填之前长什么样"了，回归核验也就失去了对照物。
    backup = path.with_suffix(".json.prebackfill")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"[info] 建立原始基线 {backup.name}（仅首次）")

    data["_stats"]["action_backfill"] = {
        "filled": filled,
        "by_confidence": dict(Counter(r[1] for r in rows)),
        "note": "derived 项为 L0(Qwen3-32B) 到位前的兜底，应被整体覆盖；low 置信项优先",
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# action 回填报告（P1.0b 兜底）", "",
             f"回填 {filled} 个镜头。**全部标记 `action_source=\"derived\"`**，",
             "是 L0 层 Qwen3-32B 到位前的兜底，不是最终内容。", "",
             "| 镜头 | 置信度 | 主体 | 回填的 action | 依据 |", "|---|---|---|---|---|"]
    for sid, conf, subj, action, why in rows:
        lines.append(f"| {sid} | {conf} | {subj} | {action} | {why} |")
    lines += ["", "## 复核优先级", "",
              "`low` 置信项是纯上下文推断（多为 OS 内心独白，原文完全没给画面），",
              "**A100 可用后应优先用 Qwen3-32B 覆盖这一批**。",
              "`high` 项来自原文括号明写的主体，可信度接近原生 action。"]
    REPORT.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n已写入 {path}（备份 {backup.name}）")
    print(f"报告 {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
