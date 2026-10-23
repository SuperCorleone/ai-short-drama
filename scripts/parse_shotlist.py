#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分镜脚本解析器：素材/小说/精神小妹也要修机甲吗/产出/05_分镜脚本.md
  -> pipeline/previs/shots.json

每个镜头输出一张"镜头规格卡 v0"：
  shot_id / group / time_range / scene(归一化枚举) / duration_s /
  shot_size / camera_move / perspective / action / dialogue / characters

容错策略：行格式不齐时尽力提取，无法识别的字段置 null 并计入统计。
幂等：直接重跑覆盖 shots.json。
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "素材" / "小说" / "精神小妹也要修机甲吗" / "产出" / "05_分镜脚本.md"
OUT = REPO_ROOT / "pipeline" / "previs" / "shots.json"

SCENES = ["卧室", "厨房", "玄关", "雪夜基地", "浴缸五金店"]
CHARACTERS = ["王天薇", "王开宇", "家用机器人", "料理机器人", "丧尸", "骆驼"]
SHOT_SIZES = ["特写", "近景", "中景", "全景", "大全景"]

# 组标题 -> 默认场景（按组号，组标题里的场景描述归一化）
GROUP_SCENE = {
    1: "卧室", 2: "卧室", 3: "卧室", 4: "雪夜基地", 5: "卧室", 6: "卧室",
    7: "卧室", 8: "厨房", 9: "厨房", 10: "厨房",
    11: "玄关", 12: "玄关", 13: "玄关", 14: "玄关",
}

# 动作文本关键词 -> 场景覆盖（闪回/转场镜头场景与组默认不同）
#
# ⚠️ 只放**固定设施与环境特征**，绝不放可携带的道具。
# 初版把「营养液」「速食面」当厨房关键词，于是 `G12-S03`
# 「他嗅了嗅空气中的食物味，目光落在她手里的**营养液**上」——
# 一个发生在玄关的镜头被判成厨房，还顺带把其后两镜也带偏（场景会向下延续）。
# 判据很简单：**这东西能不能被拿着走？** 冰箱/料理台/洗碗机搬不走，营养液能。
SCENE_OVERRIDES = [
    (["雪", "探照灯", "金属大门", "基地", "丧尸", "小队"], "雪夜基地"),
    (["浴缸", "五金店", "水花"], "浴缸五金店"),
    (["冰箱", "料理", "厨房", "洗碗", "吊柜", "灶"], "厨房"),
    (["玄关", "穿衣镜", "镜前", "镜中", "大门电子音"], "玄关"),
]

# 角色别名 -> 归一化名
CHAR_PATTERNS = [
    ("王天薇", ["王天薇", "薇姐", "原身", "她"]),
    ("王开宇", ["王开宇", "弟弟", "白毛", "高大身影", "他"]),
    ("家用机器人", ["家用机器人"]),
    ("料理机器人", ["料理机器人", "机械臂"]),
    ("丧尸", ["丧尸", "黑影"]),
    ("骆驼", ["骆驼"]),
]

# 组资产标签里出现的角色（从【资产：...】提取后过滤）
ASSET_CHARS = set(CHARACTERS)

GROUP_RE = re.compile(
    r"^###\s*第(\d+)组\s*\[([^\]]+)\]\s*([^【]*?)(?:【资产：([^】]*)】)?\s*$"
)
SHOT_RE = re.compile(r"^-\s*\[(\d+(?:\.\d+)?)s\]\s*(.*)$")


def normalize_perspective(text: str):
    """视角归一化：客观/主观/侧面/仰拍/背后，原文保留在 raw。"""
    if not text:
        return None
    for p in ["客观", "主观", "侧面", "仰拍", "背后"]:
        if p in text:
            return p
    return None


def extract_characters(text: str, group_assets):
    """从动作+台词文本抽取角色；文本无信号时回落到组资产标签中的角色。"""
    found = []
    for canon, aliases in CHAR_PATTERNS:
        if any(a in text for a in aliases):
            found.append(canon)
    if not found:
        found = [c for c in group_assets if c in ASSET_CHARS]
    # 固定顺序，保证可比对
    return [c for c in CHARACTERS if c in found]


ASSET_TAG_RE = re.compile(r"【资产：([^】]*)】")


def detect_scene(group_no: int, action_text: str, prev_scene_in_group=None) -> str:
    """判断镜头所在场景。三级判定，从强到弱。

    ⚠️ **只看画面描述（action），绝不看台词。** 初版传的是整行（动作+台词），
    于是 `G08-S04` 这个厨房里"双手与食材"的特写被判成了 `雪夜基地` ——
    因为它的 OS 台词是「这一顿搁**基地**，得半个月贡献点」。
    后果不只是环境错：场景还决定影调分线（末世线走低饱和蓝灰），
    一个厨房镜头会被套上暴雪环境 + 末世灰调，整帧废掉。
    台词提到某地 ≠ 画面在某地 —— 与 `extract_characters` 的
    「提到谁 ≠ 谁在画面里」是同一类错误。

    ② **无画面信号时沿用同组上一镜的场景**，而不是跳回组默认。
    一个组内可以换场景：第 4 组从雪夜基地闪回切到浴缸（`G04-S05`「她猛地从浴缸里
    坐起」），其后的 `G04-S06` 没有画面描述 —— 回落到组默认会把她瞬移回暴雪夜，
    而沿用上一镜才符合"镜头是连续的"这个基本事实。

    ③ 组首镜无信号才用组默认。
    """
    for keywords, scene in SCENE_OVERRIDES:
        if any(k in (action_text or "") for k in keywords):
            return scene
    if prev_scene_in_group:
        return prev_scene_in_group
    return GROUP_SCENE.get(group_no, "卧室")


# --- 台词拆解 -------------------------------------------------------------
# 分镜原文把"谁说的 + 什么情绪 + 说什么"压在一个字符串里，例如：
#   OS（疲惫沙哑，慢）"啧……床真窄啊。"
#   系统音（电子女声，温和）："您好，检测到您已经清醒……"
#   （呼吸声）
# 下游 L5 要按说话人分配音色、按情绪调 TTS 风格，必须拆开。
# 同样重要的是把台词**从 action 里摘出去**——action 会被拼进 L3 的图像提示词，
# 混进台词等于给扩散模型喂噪声。
SPEAKER_RE = re.compile(
    r"^(?P<speaker>OS|[一-龥A-Za-z]{2,8})"
    r"[（(](?P<emotion>[^）)]*)[）)]"
    r"\s*[:：]?\s*"
    r"[\"“](?P<text>.*?)[\"”]\s*$"
)
# 纯音效行：整行只有一个括号注记，无引号文本
SFX_RE = re.compile(r"^[（(](?P<sfx>[^）)]*)[）)]\s*$")

# 说话人 → 音色类型。决定 L5 用哪路 TTS 音色。
VOICE_TYPES = {
    "OS": "os",            # 内心独白：主角音色，近讲、干声、低能量
    "系统音": "system",
    "光脑新闻音": "broadcast",
    "画面音": "broadcast",
    "料理机器人": "robot",
    "家用机器人": "robot",
}


# 台词**内嵌在动作中间**的形态：`…（语气）"台词"…`
#
# 2026-08-10 由 L1 评估反查发现：修完"整体是台词"的分支后，仍有 2 条漏网 ——
#   G13-S03  王开宇坐门口脱战术靴（随意）"S班的集训结束了。…"     动作在前、台词在后
#   G06-S05  OS（老兵式无语，一字一顿）"都星际时代了…"她扶额，…    台词在前、动作在后
# 两条的 `dialogue` 都是 None，也就是**台词从没被提取过**：既没进 L5 配音，
# 又留在 action 里被拼进 L3 图像提示词当噪声。
# clip_t 把 G13-S03 排到 82 张里的最低分（0.1830）—— **指标反过来抓出了数据缺陷**。
#
# 关键是**别误伤**：`音效"嘀嘀"` 与 `字幕卡："…"` 也带引号但都不是台词。
# 判据：引号必须**紧邻在 `（语气）` 之后**。音效与字幕卡前面没有这个括号。
INLINE_DIALOGUE_RE = re.compile(r"（(?P<emotion>[^（）]{1,20})）[\"“](?P<text>[^\"”]+)[\"”]")


def split_dialogue(action: str):
    """把误并进 action 的台词摘出来。返回 (纯动作|None, 台词原串|None)。

    两种形态都处理：
      ① action **整体**就是一句台词 → 整条搬走
      ② 台词**内嵌**在动作里（`…（语气）"台词"…`）→ 摘出台词，动作留下
    形如"她盯着舱顶叹气｜OS（无奈）..."的行本来就已被 ｜ 正确分段，不走这里。
    """
    if not action:
        return action, None
    a = action.strip()
    if SPEAKER_RE.match(a) or SFX_RE.match(a):
        return None, a

    m = INLINE_DIALOGUE_RE.search(a)
    if m:
        head = a[:m.start()].strip()
        tail = a[m.end():].strip()
        # 说话人 = 括号之前那段里出现的角色名（含 OS）；找不到就不猜，留 raw 给人工
        speaker = None
        for canon, aliases in CHAR_PATTERNS:
            if any(al in head for al in aliases if len(al) >= 2):
                speaker = canon
                break
        if head.startswith("OS"):
            speaker = "OS"
        if speaker:
            # 动作 = 头部去掉说话人名 + 尾部
            act_head = head[len("OS"):] if speaker == "OS" else head.replace(speaker, "", 1)
            act = "，".join(p for p in (act_head.strip("，。 、"), tail.strip("，。 、")) if p)
            raw = f'{speaker}（{m.group("emotion")}）"{m.group("text")}"'
            return (act or None), raw
    return action, None


def parse_dialogue(raw: str):
    """把台词原串拆成 speaker / voice_type / emotion / text。

    拆不动时不猜：speaker=None 且保留 raw，交由人工回填，
    绝不静默丢弃（丢台词在成片阶段才发现，代价太大）。
    """
    if not raw:
        return None
    s = raw.strip()
    m = SPEAKER_RE.match(s)
    if m:
        speaker = m.group("speaker")
        return {
            "speaker": speaker,
            "voice_type": VOICE_TYPES.get(speaker, "character"),
            "emotion": (m.group("emotion") or "").strip() or None,
            "text": m.group("text").strip(),
            "raw": s,
        }
    m = SFX_RE.match(s)
    if m:
        return {"speaker": None, "voice_type": "sfx", "emotion": None,
                "text": None, "sfx": m.group("sfx").strip(), "raw": s}
    return {"speaker": None, "voice_type": "unparsed", "emotion": None,
            "text": None, "raw": s}


def grade_shot(size, has_speech, is_transition):
    """重抽预算分级（TASKS.md 长期纪律：普通镜头 2 次、S 级 3 次封顶）。

    规则从简且可解释：带台词的特写/近景是观众注意力焦点 → S；
    纯转场不值得重抽 → C；其余 B。
    """
    if is_transition:
        return "C"
    if has_speech and size in ("特写", "近景"):
        return "S"
    return "B"


def parse_shot_line(body: str):
    """解析 `[Ns]` 后的正文。返回 (fields_dict, is_transition)。

    标准形：景别｜运镜｜视角：动作｜台词
    容错：缺 ｜ 分段、缺视角、转场/黑屏/字幕卡行。
    """
    parts = [p.strip() for p in body.split("｜")]
    size = move = persp_raw = action = dialogue = None
    is_transition = False

    subject_hint = None
    if len(parts) >= 2 and any(s in parts[0] for s in SHOT_SIZES + ["蒙太奇"]):
        head = parts[0]
        for s in SHOT_SIZES:
            if s in head:
                size = s
                break
        # 景别括号注记里的主体，如 "近景（王开宇）"、"特写（镜中）"
        hm = re.search(r"[（(]([^）)]*)[）)]", head)
        if hm:
            subject_hint = hm.group(1).strip() or None
        rest = parts[1:]
        # 找 "视角：动作" 段
        for i, seg in enumerate(rest):
            if "：" in seg and any(p in seg.split("：", 1)[0] for p in
                                   ["客观", "主观", "侧面", "仰拍", "背后"]):
                persp_raw, action = seg.split("：", 1)
                persp_raw = persp_raw.strip()
                action = action.strip()
                move = "｜".join(rest[:i]).strip() or None
                dialogue = "｜".join(rest[i + 1:]).strip() or None
                break
        else:
            # 没有显式视角段：第2段当运镜，再往后是动作/台词
            move = rest[0] or None
            if len(rest) >= 2:
                action = rest[1]
                dialogue = "｜".join(rest[2:]).strip() or None
    else:
        # 转场/黑屏/字幕卡等非标准行
        is_transition = True
        action = body.strip()
        if "：" in body:
            left, right = body.split("：", 1)
            if len(left) <= 12:
                action = body.strip()

    # action 整体就是一句台词时搬回 dialogue（修复 36/82 镜头的错分，见 split_dialogue）
    if not dialogue:
        action, moved = split_dialogue(action)
        if moved:
            dialogue = moved

    speech = parse_dialogue(dialogue)
    has_speech = bool(speech and speech.get("text"))

    # 【资产：xxx】是**标注语法**，不是画面描述。它会随 action 被拼进 L3 图像提示词，
    # 而扩散模型看到「【资产：雪夜基地大门】」只会当成一串噪声，甚至试图把方括号
    # 和文字渲进画面。摘出来单独存，action 只留干净的画面描述。
    shot_assets = []
    if action:
        shot_assets = [a.strip() for a in ASSET_TAG_RE.findall(action) if a.strip()]
        action = ASSET_TAG_RE.sub("", action).strip("，。 、") or None

    return {
        "shot_size": size,
        "subject_hint": subject_hint,
        "camera_move": move,
        "perspective": normalize_perspective(persp_raw),
        "perspective_raw": persp_raw,
        "action": action,
        "shot_assets": shot_assets,
        "dialogue": dialogue,
        "speech": speech,
        "grade": grade_shot(size, has_speech, is_transition),
    }, is_transition


def main():
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines()

    shots = []
    stats = {
        "source": str(SRC.relative_to(REPO_ROOT)),
        "groups": 0,
        "lines_total": len(lines),
        "shots_parsed": 0,
        "shots_with_full_fields": 0,
        "transition_shots": 0,
        "shots_missing_fields": [],
        "scenes": {},
        "characters_seen": {},
    }

    group_no = None
    group_title = ""
    time_range = ""
    group_assets = []
    shot_idx = 0

    for line in lines:
        line = line.rstrip()
        m = GROUP_RE.match(line)
        if m:
            group_no = int(m.group(1))
            time_range = m.group(2).strip()
            group_title = m.group(3).strip()
            assets_raw = m.group(4) or ""
            group_assets = [a.strip() for a in re.split(r"[/、，,]", assets_raw) if a.strip()]
            shot_idx = 0
            prev_scene_in_group = None      # 场景延续只在组内有效，跨组重置
            stats["groups"] += 1
            continue

        s = SHOT_RE.match(line)
        if s and group_no is not None:
            shot_idx += 1
            duration = float(s.group(1))
            body = s.group(2).strip()
            fields, is_transition = parse_shot_line(body)

            full_text = body
            # characters 保持原语义（"这一行提到了谁"，含台词）—— 下游 build_previs.mjs
            # 在消费它，改语义会静默改变预演管线的行为。画面主体另由
            # scripts/backfill_action.py 的 `subject` 字段负责。
            characters = extract_characters(full_text, group_assets)
            # 场景**只从画面描述判断**，无信号时沿用同组上一镜（见 detect_scene）
            scene = detect_scene(group_no, fields["action"] or "", prev_scene_in_group)
            prev_scene_in_group = scene

            shot_id = f"G{group_no:02d}-S{shot_idx:02d}"
            card = {
                "shot_id": shot_id,
                "group": group_no,
                "group_title": group_title,
                "time_range": time_range,
                "scene": scene,
                "duration_s": duration,
                "shot_size": fields["shot_size"],
                "subject_hint": fields["subject_hint"],
                "camera_move": fields["camera_move"],
                "perspective": fields["perspective"],
                "perspective_raw": fields["perspective_raw"],
                "action": fields["action"],
                "shot_assets": fields["shot_assets"],
                "dialogue": fields["dialogue"],
                "speech": fields["speech"],
                "grade": fields["grade"],
                "characters": characters,
                "is_transition": is_transition,
                "raw_line": line,
            }
            shots.append(card)

            stats["shots_parsed"] += 1
            if is_transition:
                stats["transition_shots"] += 1
            sp = fields["speech"]
            if sp:
                stats["shots_with_speech"] = stats.get("shots_with_speech", 0) + 1
                vt = sp["voice_type"]
                stats.setdefault("voice_types", {})
                stats["voice_types"][vt] = stats["voice_types"].get(vt, 0) + 1
                if vt == "unparsed":
                    stats.setdefault("speech_unparsed", []).append(
                        {"shot_id": shot_id, "raw": sp["raw"]})
            stats.setdefault("grades", {})
            stats["grades"][fields["grade"]] = stats["grades"].get(fields["grade"], 0) + 1
            missing = [k for k in ["shot_size", "camera_move", "perspective", "action"]
                       if not fields[k]]
            if not missing:
                stats["shots_with_full_fields"] += 1
            else:
                stats["shots_missing_fields"].append({"shot_id": shot_id, "missing": missing})
            stats["scenes"][scene] = stats["scenes"].get(scene, 0) + 1
            for c in characters:
                stats["characters_seen"][c] = stats["characters_seen"].get(c, 0) + 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_stats": stats, "shots": shots}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"parsed {stats['shots_parsed']} shots from {stats['groups']} groups -> {OUT}")
    print(f"  full-field shots: {stats['shots_with_full_fields']}, "
          f"transitions: {stats['transition_shots']}, "
          f"missing-field shots: {len(stats['shots_missing_fields'])}")
    print(f"  scenes: {stats['scenes']}")
    print(f"  characters: {stats['characters_seen']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
