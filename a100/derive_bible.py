#!/usr/bin/env python3
"""P1.3c · 角色圣经派生（A100 · Qwen-Image-Edit-2511）。

以本机锁定的基准脸为参考图，派生三视图 / 表情集 / 服装表。
本地 4070 装不下 20B，故此步只能在 A100 上做（ARCHITECTURE.md L1 层）。

⚠️ 提示词一律显式写族裔：2026-08-08 实测 Qwen-Image 系**不默认生成亚洲人**，
同一句"年轻女性"，Z-Image 出亚洲人、Qwen-Image-2512 出白人。

用法（在 icsnode06 上，ComfyUI 已长驻于 127.0.0.1:8189）：
    python3 a100/derive_bible.py --out-root /scratch/gaor/ai_short_drama/outputs/bible
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, "workflows", "bible.json")
COMFY = "http://127.0.0.1:8189"

# 锁定基准脸（相对 ComfyUI input/），来自本机 /faces 门禁选出的最优组合
# （跨角色 ArcFace 0.4106，红线 0.65）
CHARACTERS = {
    "wang_tianwei": {
        "ref": "bible/wang_tianwei_s1001_00001_.png",
        "who": "这位18岁亚洲少女，娃娃脸，长发染成黑白渐变（发根乌黑、发梢纯白），素颜无妆",
    },
    "wang_kaiyu": {
        "ref": "bible/wang_kaiyu_s1002_00001_.png",
        "who": "这位17岁亚洲少年，体格高大结实，利落短发，神情冷淡",
    },
    "fu_xingyuan": {
        "ref": "bible/fu_xingyuan_s1000_00001_.png",
        "who": "这位18岁亚洲少年，一头自然纯白短发，五官清冷精致",
    },
}

# 派生项：(组, 名, 指令)。指令一律以「保持人物身份与五官完全不变」开头——
# 这是身份不漂的关键，也是下游 ArcFace >=0.75 门禁能过的前提。
KEEP = "保持人物身份与五官完全不变，只改变"
# 表情项专用约束：ArcFace 掉分的根因是骨相被改，不是表情本身。
# 明确把可变范围限制在肌肉层，脸型/鼻子/下颌轮廓一律锁死。
ONLY_MUSCLE = "**脸型、鼻子、下颌轮廓、五官比例一律不变**，仅改变眉毛与嘴部肌肉的"
DERIVATIONS = [
    ("views", "front", f"{KEEP}姿态：正面全身站姿，双臂自然下垂，均匀柔光，纯浅灰背景"),
    ("views", "side", f"{KEEP}姿态：正侧面全身站姿（面朝画面右侧），均匀柔光，纯浅灰背景"),
    ("views", "back", f"{KEEP}姿态：**完全背对镜头**的全身站姿，头部朝正后方，**看不到脸、看不到任何五官**，只见后脑与背影，均匀柔光，纯浅灰背景"),
    ("expressions", "neutral", f"{KEEP}表情：平静中性，直视镜头，齐胸半身"),
    ("expressions", "surprised", f"{KEEP}{ONLY_MUSCLE}表情：惊讶——眼睛睁大、眉毛上扬，齐胸半身正面"),
    ("expressions", "angry", f"{KEEP}{ONLY_MUSCLE}表情：愠怒——眉毛压低内聚、嘴角下压，**不要皱鼻、不要鼓腮、不要眯眼**，齐胸半身正面"),
    ("expressions", "tired", f"{KEEP}{ONLY_MUSCLE}表情：疲惫——上眼睑略垂、目光放空，齐胸半身正面"),
    ("expressions", "smile", f"{KEEP}表情：轻微微笑，嘴角上扬，齐胸半身"),
    ("outfits", "home", f"{KEEP}服装：宽松居家棉质长袖上衣与**长裤**（不是裙子），膝盖以上取景，脸部清晰可辨，纯浅灰背景"),
    ("outfits", "tactical", f"{KEEP}服装：深色战术服上衣与**战术长裤**（不是裙子），多口袋，战术靴，膝盖以上取景，脸部清晰可辨，纯浅灰背景"),
    ("outfits", "uniform", f"{KEEP}服装：浅灰军校制服上衣与**制服长裤**（不是裙子），肩章，膝盖以上取景，脸部清晰可辨，纯浅灰背景"),
]

NEGATIVE = ("换脸, 不同的人, 五官改变, 变形, 多余手指, 低分辨率, 模糊, 水印, 文字, "
            "过曝, 塑料感皮肤, 欧美面孔, 白人面孔, 裙子, 短裙, 连衣裙")


def post(path, payload, timeout=1800):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        COMFY + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=120):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run(ref, prompt, seed, out_name, timeout=1800):
    raw = open(WORKFLOW, encoding="utf-8").read()
    raw = (raw.replace("__REF_IMAGE__", ref)
              .replace("__PROMPT__", json.dumps(prompt, ensure_ascii=False)[1:-1])
              .replace("__NEGATIVE__", json.dumps(NEGATIVE, ensure_ascii=False)[1:-1])
              .replace("__SEED__", str(seed))
              .replace("__OUT_NAME__", out_name))
    wf = json.loads(raw)
    pid = post("/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = get(f"/history/{pid}")
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError(json.dumps(st.get("messages", []), ensure_ascii=False)[:600])
            outs = [o for n in h[pid].get("outputs", {}).values() for o in n.get("images", [])]
            if not outs:
                raise RuntimeError("无产出")
            return outs[0], round(time.time() - t0, 1)
        time.sleep(2)
    raise TimeoutError(out_name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", default="/scratch/gaor/ai_short_drama/outputs/bible")
    ap.add_argument("--seed", type=int, default=100)
    ap.add_argument("--only", help="只跑指定角色，逗号分隔")
    ap.add_argument("--redo", help="强制重出这些 key（char/group/name，逗号分隔），忽略 manifest 里的已完成记录")
    args = ap.parse_args()

    os.makedirs(args.out_root, exist_ok=True)
    manifest = os.path.join(args.out_root, "manifest.jsonl")
    done = set()
    if os.path.exists(manifest):
        for line in open(manifest, encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") == "ok":
                done.add(r["key"])

    chars = args.only.split(",") if args.only else list(CHARACTERS)
    jobs = [(c, g, n, p) for c in chars for g, n, p in DERIVATIONS]
    if args.redo:
        redo = set(args.redo.split(","))
        todo = [j for j in jobs if f"{j[0]}/{j[1]}/{j[2]}" in redo]
        missing = redo - {f"{j[0]}/{j[1]}/{j[2]}" for j in jobs}
        if missing:
            print(f"[warn] --redo 里有无法匹配的 key: {sorted(missing)}")
    else:
        todo = [j for j in jobs if f"{j[0]}/{j[1]}/{j[2]}" not in done]
    print(f"派生 {len(jobs)} 项，已完成 {len(done)}，待跑 {len(todo)}", flush=True)

    ok = failed = 0
    with open(manifest, "a", encoding="utf-8") as log:
        for i, (char, group, name, instr) in enumerate(todo, 1):
            key = f"{char}/{group}/{name}"
            spec = CHARACTERS[char]
            prompt = f"{spec['who']}。{instr}。写实人像摄影，高细节。"
            rec = {"key": key, "char": char, "group": group, "name": name, "seed": args.seed}
            try:
                out, secs = run(spec["ref"], prompt, args.seed, f"{char}_{group}_{name}")
                rec.update(status="ok", output=out, seconds=secs)
                ok += 1
            except Exception as e:
                rec.update(status="failed", error=f"{type(e).__name__}: {e}"[:400])
                failed += 1
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            os.fsync(log.fileno())
            mark = "OK  " if rec["status"] == "ok" else "FAIL"
            print(f"[{i}/{len(todo)}] {mark} {key} ({rec.get('seconds','-')}s)", flush=True)
            if rec["status"] == "failed":
                print(f"      {rec['error']}", flush=True)

    print(f"\n成功 {ok} / 失败 {failed} -> {manifest}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
