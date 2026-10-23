#!/usr/bin/env python3
"""L3 图像提示词模板：镜头规格卡 → 关键帧提示词。

单独成模块而不是塞在 `run_zimage.py` 里，因为**同一套提示词要喂两个模型**：
本机 4070 的 Z-Image（代理帧）与 A100 的 Qwen-Image-2512（终版帧）。
两边必须用同一个模板，否则代理阶段锁定的构图在终渲时会漂掉 ——
而"迭代在代理上做、终渲在 A100 上做"（架构定律 2）的全部前提就是两者一致。

## 模板必须编码的四件事

1. **族裔显式化**。TASKS.md 2026-08-08 实测：同一句"一位二十岁出头的年轻女性"，
   Z-Image 给亚洲女性、**Qwen-Image-2512 给白人女性**。做中文短剧时若不写死族裔，
   换到 A100 上整集人物会全歪。所以每条含人物的提示词都显式写"亚洲"，
   负面词里再挡一道"欧美面孔"。

2. **角色外观锁定**。角色一致性的第一道防线是提示词本身（第二道才是角色圣经参考图
   与 ArcFace 门禁）。王天薇的「娃娃脸 + 黑白渐变发 + 素颜」是剧情点，不是装饰：
   「一周没化妆」是原文明写的情节，图资产必须用素颜版。

3. **双线影调**。素材定的是星际线「干净冷亮的冷色调」vs 末世闪回线「低饱和蓝灰、
   粗粝压抑」，两条线靠影调区分以免观众混淆。这个必须进提示词，
   否则闪回和现实会长得一模一样。

4. **9:16 竖屏**。平台是红果/抖音信息流。

## 刻意不进提示词的东西

- **运镜**（固定/缓推/手持）：静帧里没有运动，写进去只是噪声。它的用武之地在 L4 I2V。
- **`speech.emotion` 的音质与语速**（沙哑/渐快）：那是 TTS 的参数。
  `backfill_action.py` 已在上游把两者切开，这里只消费清洗后的 action。

自检: python pipeline/keyframe/l3_prompt.py --selftest
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHARACTERS_JSON = os.path.join(ROOT, "pipeline", "assets", "characters.json")

# 角色外观锁定短语。比 characters.json 里的 baseline_prompt 短 ——
# baseline_prompt 是给"基准脸"用的（正面/中性/纯背景），镜头里要留出
# 篇幅给场景与动作，塞完整版会稀释掉画面描述的权重。
CHARACTER_BRIEF = {
    "王天薇": "18岁亚洲少女，娃娃脸，圆润脸型，幼态大眼，长发黑白渐变（发根乌黑、发梢纯白），素颜无妆",
    "王开宇": "17岁亚洲少年，身高近两米，体格高大结实，利落短发，眉眼锋利，下颌线分明，深色战术风着装",
    "傅星渊": "18岁亚洲少年，一头自然纯白短发，五官清冷精致，眼型狭长",
    "家用机器人": "圆滚滚的白色家用机器人，眼部呼吸灯明灭",
    "料理机器人": "白色圆润机身的料理机器人，六条可伸缩机械臂",
    "丧尸": "五级丧尸，人形轮廓但扭曲异化，长舌生倒刺，恐怖压迫感",
    "骆驼": "末世丧尸围剿小队的年轻男性队员，脏污战术装，满脸涕泪",
}

# 场景环境。取自 02_资产提取.md 的场景清单，保留标志性视觉符号
# （睡眠舱/智脑舱是"标志性视觉符号，参考图需单独出"，必须每次出现）。
SCENE_ENV = {
    "卧室": "星际时代的卧室，冷色调高科技家居，并排立着灰白色睡眠舱与智脑接入舱（两米长、形似白色棺材），白色沙发",
    "厨房": "深夜的智能厨房，金属质感温感地板，感应灯，大冰箱与料理台，吊柜",
    "玄关": "深夜的玄关，大面穿衣镜，感应灯，电子门",
    "雪夜基地": "末世暴雪夜，基地的巨大金属门，探照灯光柱乱晃，风雪",
    "浴缸五金店": "末世回忆，昏暗的五金店与一只旧浴缸，热气蒸腾",
}

# 特写/近景用的**精简**环境描述。
#
# 🐛 目检发现：`G01-S01` 明明是特写，出图却是中景 —— 因为完整环境句罗列了
# 「睡眠舱 + 智脑接入舱 + 白色沙发」，这份家具清单本身就在邀请模型把镜头拉远，
# 力量盖过了后面那句「特写镜头，主体充满画面」。
# **景别必须调制环境描述的详细程度**：特写里环境本来就只剩一点虚化背景，
# 把陈设一一列出只会跟景别打架。这是提示词工程里"两句都对、放一起就错"的典型。
SCENE_ENV_TIGHT = {
    "卧室": "星际时代卧室的虚化背景，冷色调高科技质感",
    "厨房": "智能厨房的虚化背景，金属冷光",
    "玄关": "玄关的虚化背景，穿衣镜边缘与感应灯",
    "雪夜基地": "暴雪与探照灯光的虚化背景",
    "浴缸五金店": "昏暗浴室的虚化背景，蒸汽",
}
TIGHT_SIZES = {"特写", "近景"}

# 末世闪回线 —— 影调必须与现实线拉开，否则观众分不清时间线
FLASHBACK_SCENES = {"雪夜基地", "浴缸五金店"}
TONE_PRESENT = "干净冷亮的冷色调影调，高科技质感"
TONE_FLASHBACK = "低饱和蓝灰影调，粗粝压抑，颗粒感"

SHOT_SIZE_FRAMING = {
    "特写": "特写镜头，主体充满画面，浅景深",
    "近景": "近景，胸部以上",
    "中景": "中景，腰部以上",
    "全景": "全景，人物全身与环境同框",
    "大全景": "大全景，环境为主，人物占比小",
}

BASE_STYLE = "电影感打光，写实摄影，高细节，真实皮肤质感，竖屏9:16构图"

# 由 scripts/backfill_action.py 生成的主观视角前缀。两处共用这个常量，
# 免得一边改措辞另一边静默失配（那种失配不会报错，只会让画面悄悄错掉）。
POV_MARKER = "第一人称主观视角"

# 主观视角下的**视点身份**。
#
# 🐛 第一版把角色外观描述整个删掉（因为她在镜头后面），结果 `G01-S02` 出图是
# 一双**男性多毛的腿** —— 主角是 18 岁少女。删干净解决了"不该出现她的脸"，
# 却制造了"入画的手脚不是她的"。
# 正解是分开处理：**不描述脸与头发**（那才是"在镜头后面"的含义），
# **但必须说明视点属于谁**，否则模型对入画肢体没有任何约束，默认男性。
CHARACTER_POV_SELF = {
    "王天薇": "视点属于一位18岁亚洲少女，入画的手脚是纤细白皙的少女手脚",
    "王开宇": "视点属于一位17岁亚洲少年，入画的手脚是高大结实的少年手脚",
    "傅星渊": "视点属于一位18岁亚洲少年",
}

# action 里出现这些词，说明分镜对光线有明确要求，通用影调样板句必须让路
LIGHTING_WORDS = ["漆黑", "昏暗", "黑暗", "冷光", "探照灯", "闪白", "亮起", "光屏", "逆光", "暗"]

# POV 镜头里出现这些词，说明自己的肢体会入画，此时才需要交代视点身份
LIMB_WORDS = ["手", "脚", "腿", "身体", "指甲", "掌"]

# 负面词分三段：通用低质 + AI 味 + **族裔护栏**。
# 族裔那段是 2026-08-08 A100 实测教训的产物，不是想当然加的。
NEGATIVE_BASE = (
    "lowres, blurry, jpeg artifacts, watermark, text overlay, extra fingers, "
    "deformed hands, bad anatomy, oversaturated, plastic skin, cgi look, "
    "flat lighting, cluttered composition, 3d render, illustration"
)
NEGATIVE_ETHNIC = "欧美面孔, 白人面孔, 混血脸"


def _load_negative_extra():
    """从角色圣经取每个角色的专属负面词（如王天薇的「浓妆/眼线」）。"""
    try:
        data = json.load(open(CHARACTERS_JSON, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {c["name"]: c.get("negative_extra", "") for c in data.get("characters", [])}


NEGATIVE_EXTRA = _load_negative_extra()


def build(shot):
    """镜头规格卡 → (prompt, negative)。

    `subject` 与清洗后的 `action` 由 `scripts/backfill_action.py` 保证存在。
    """
    scene = shot.get("scene") or ""
    action = (shot.get("action") or "").strip()
    subject = shot.get("subject") or ""
    size = shot.get("shot_size") or ""

    # 出场人物 = 画面主体 + action 文本里点到名的角色。
    # **不能用 `characters` 字段** —— 它的语义是"这句台词提到了谁"，
    # 会把只在旁白里被提及的角色（如闪回台词里的丧尸）当成在场人物。
    present = []
    if subject in CHARACTER_BRIEF:
        present.append(subject)
    for name in CHARACTER_BRIEF:
        if name != subject and name in action:
            present.append(name)

    # 🐛 主观视角：**她在镜头后面，不在画面里。**
    # 首版没排除，于是 `G01-S02`（原文写着「主观（舱顶视角）」）的提示词里
    # 照样拼进了「18岁亚洲少女，娃娃脸，黑白渐变长发…」—— 模型当然就把她画了出来，
    # 出图是一张正面肖像而不是她眼中的舱顶。目检才发现，提示词单看毫无异常。
    pov = action.startswith(POV_MARKER)
    if pov and subject in present:
        present.remove(subject)

    parts = []
    # POV 也用精简环境：主观镜头看的是某个具体的东西（舱顶、手里的杯子），
    # 把整个房间的陈设列一遍会把它拉成一个环境全景
    env_table = SCENE_ENV_TIGHT if (size in TIGHT_SIZES or pov) else SCENE_ENV
    if scene in env_table:
        parts.append(env_table[scene])
    if pov:
        # 视点身份只在**画面确实会出现肢体**时才写。
        # 🐛 无条件写的那一版把 `G01-S02` 变成了一张脚部特写 ——
        # "入画的手脚是纤细白皙的少女手脚"喧宾夺主，模型拿它当了主体，
        # 而这镜的意图是躺在舱内仰望舱盖。POV 里不提人物时必须**明说不出现人物**，
        # 否则模型会自己找一个主体填进来（第一版填的是男性的腿）。
        if any(k in action for k in LIMB_WORDS):
            parts.append(CHARACTER_POV_SELF.get(subject, ""))
        else:
            parts.append("画面中不出现任何人物，纯粹是眼前所见的景物")
    for name in present:
        parts.append(CHARACTER_BRIEF[name])
    if action:
        parts.append(action)
    if size in SHOT_SIZE_FRAMING and not pov:
        parts.append(SHOT_SIZE_FRAMING[size])

    # 影调样板句只在 action **没有自带光线描述**时才加。
    # 否则会打架：`G01-S01` 的 action 是「漆黑舱内」，而现实线样板句是
    # 「干净冷亮的冷色调影调」—— 两句同时喂进去，出图是亮的，"漆黑"被压掉了。
    # 具体描述应当压过通用样板，而不是相反。
    if not any(k in action for k in LIGHTING_WORDS):
        parts.append(TONE_FLASHBACK if scene in FLASHBACK_SCENES else TONE_PRESENT)
    parts.append(BASE_STYLE)

    neg = [NEGATIVE_BASE, NEGATIVE_ETHNIC]
    for name in present:
        extra = NEGATIVE_EXTRA.get(name)
        if extra:
            neg.append(extra)
    if pov:
        neg.append("人物正脸, 自拍, 肖像, 镜头前有人")
    return "，".join(p for p in parts if p), ", ".join(neg)


def _selftest():
    path = os.path.join(ROOT, "pipeline", "previs", "shots.json")
    shots = json.load(open(path, encoding="utf-8"))["shots"]
    picks = ["G01-S01", "G01-S02", "G03-S04", "G08-S04", "G11-S02", "G12-S02", "G13-S05"]
    by_id = {s["shot_id"]: s for s in shots}
    fails = 0
    for sid in picks:
        s = by_id[sid]
        p, n = build(s)
        print(f"── {sid}  [{s.get('scene')} / {s.get('shot_size')} / {s.get('action_source')}]")
        print(f"   {p}")
        print(f"   NEG: {n}\n")

    # 不变量：每条提示词都必须落到族裔护栏上，否则 A100 上会出白人
    for s in shots:
        p, n = build(s)
        if "欧美面孔" not in n:
            print(f"[FAIL] {s['shot_id']} 负面词缺族裔护栏")
            fails += 1
        if not p.strip():
            print(f"[FAIL] {s['shot_id']} 提示词为空")
            fails += 1
        subj = s.get("subject")
        # POV 镜头画面里没有人物，族裔不适用 —— 不排除的话这条不变量会误报
        is_pov = (s.get("action") or "").startswith(POV_MARKER)
        if (not is_pov and subj in ("王天薇", "王开宇", "傅星渊") and "亚洲" not in p):
            print(f"[FAIL] {s['shot_id']} 人物镜头未显式写族裔")
            fails += 1
        if is_pov and "人物正脸" not in n:
            print(f"[FAIL] {s['shot_id']} POV 镜头负面词缺「人物正脸」护栏")
            fails += 1
    lens = [len(build(s)[0]) for s in shots]
    print(f"提示词长度: 最短 {min(lens)} / 中位 {sorted(lens)[len(lens)//2]} / 最长 {max(lens)} 字")
    print(f"自检 {len(shots)} 条镜头：{'全部通过' if not fails else str(fails) + ' 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
