#!/usr/bin/env python3
"""S2 · MiniMax-H3 Ref2VA 角色一致性验证。

要回答的问题：**Ref2VA 靠参考图能不能替代 LoRA 保住角色一致性？**
如果能，RQ3 里的 LoRA 训练臂可以整个砍掉，省下以天计的 A100 时间。

方法：拿已锁定的角色圣经（三视图/表情/服装，≤9 张）当 ref_images，
生成该角色的镜头视频，再逐帧 ArcFace 比对基准脸。

length 必须落在模型的 17k+5 网格上（节点 tooltip 明示，训练范围 ~124–362）：
    124 = 17*7+5  ≈ 5.2s @24fps
    226 = 17*13+5 ≈ 9.4s
    362 = 17*21+5 ≈ 15.1s

用法（icsnode06，ComfyUI 长驻于 127.0.0.1:8189）：
    python3 a100/run_h3_ref2va.py --shots G01-S01,G02-S01,G11-S01 --width 832 --height 480 --length 124
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW = os.path.join(HERE, "workflows", "h3_ref2va.json")
COMFY = "http://127.0.0.1:8189"

# 王天薇的角色圣经参考集（相对 ComfyUI input/）。
# 只挑正面向、脸清晰的项——侧/背面对身份锚定没帮助，还占掉宝贵的 9 张额度。
REF_SETS = {          # 每个角色 4 张：基准脸 + 正面视图 + 中性表情 + 居家服
    c: [f"bible/{c}_{k}_00001_.png" for k in (base, "views_front",
                                              "expressions_neutral", "outfits_home")]
    for c, base in (("wang_tianwei", "s1001"), ("wang_kaiyu", "s1002"),
                    ("fu_xingyuan", "s1000"))
}
NAME2KEY = {"王天薇": "wang_tianwei", "王开宇": "wang_kaiyu", "傅星渊": "fu_xingyuan"}
REF_SET = REF_SETS["wang_tianwei"]      # 兼容旧调用

# 提示词复用 L3 的构建器（本机与 A100 同一份），不再维护第二套
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "pipeline", "keyframe"))
import l3_prompt

NEG_HINT = ("欧美面孔, 白人面孔, 换脸, 五官改变, 低分辨率, 模糊, 水印, 文字")


def post(path, payload, timeout=7200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        COMFY + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path, timeout=180):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def snap_length(seconds, fps=24):
    """把秒数吸附到模型的 17k+5 帧网格。"""
    n = max(5, round(seconds * fps))
    n += (5 - (n % 17)) % 17
    return n


def build(prompt, width, height, length, steps, seed, out_name, refs):
    wf = json.loads(open(WORKFLOW, encoding="utf-8").read())
    wf.pop("_comment", None)
    n20 = wf["20"]["inputs"]
    n20["prompt"] = prompt
    n20["width"] = width
    n20["height"] = height
    n20["length"] = length
    # AUTOGROW 输入的键名是**点号拼接**的 `<输入名>.<prefix><i>`，即 `ref_images.ref_image_0`。
    # 依据是 ComfyUI 源码 `comfy_api/latest/_io.py` 的 `_expand_schema_for_dynamic()`：
    #     expected_id = finalize_prefix(curr_prefix, name)   # finalize_prefix 用 "." 连接
    #     if expected_id in live_inputs: ...                 # 只有这个键名才会被收集
    # 引擎收集后才以 execute(ref_images={...}) 传给节点（节点侧是 `.values()` 遍历）。
    #
    # 🐛 2026-08-11 连踩两种错误形式，两者的失败方式截然不同，都值得记住：
    #   ① `n20["ref_images"] = {"ref_image_0": [nid,0], ...}`（嵌套 dict）
    #      → ComfyUI **只在 inputs 顶层解析 [node_id, slot] 链接**，嵌套的不解析，
    #        LoadImage 因连不到输出被剪枝、从未执行。**不报错、不告警、照常出片、零效果** ——
    #        最危险的一种，整轮 S2 实际跑的是纯文本生成，却差点被记成"H3 保不住身份"的能力结论。
    #   ② `n20["ref_image_0"] = [nid,0]`（顶层平铺）
    #      → 链接解析了、LoadImage 执行了，但引擎不做收集，直接当普通 kwarg 传下去，
    #        `TypeError: got an unexpected keyword argument 'ref_image_0'`。**这种会响，反而安全。**
    #
    # 验收判据不能只看 ArcFace 上没上去（那还可能是"参考图作用弱"）：
    # **必须比 `--n-refs 0` 对照臂的视频流 MD5** —— 相同即证明参考图完全没进模型。
    for i, rel in enumerate(refs[:9]):
        nid = str(100 + i)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": rel, "upload": "image"}}
        n20[f"ref_images.ref_image_{i}"] = [nid, 0]
    wf["23"]["inputs"]["steps"] = steps
    wf["24"]["inputs"]["noise_seed"] = seed
    wf["29"]["inputs"]["filename_prefix"] = f"h3ref/{out_name}"
    return wf


def run(wf, timeout=7200):
    pid = post("/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = get(f"/history/{pid}")
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError(json.dumps(st.get("messages", []), ensure_ascii=False)[:1500])
            outs = []
            for node in h[pid].get("outputs", {}).values():
                for key in ("images", "videos", "gifs"):
                    for item in node.get(key, []):
                        outs.append(os.path.join(item.get("subfolder", ""), item["filename"]))
            return outs, round(time.time() - t0, 1)
        time.sleep(3)
    raise TimeoutError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="ALL", help="逗号分隔 shot_id")
    ap.add_argument("--shots-json", default="/home/gaor/ai_short_drama/pipeline/previs/shots.json")
    ap.add_argument("--width", type=int, default=832)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--seconds", type=float, default=0.0, help="给秒数则自动吸附到 17k+5；否则用 --length")
    ap.add_argument("--length", type=int, default=124)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="/scratch/gaor/ai_short_drama/outputs/h3_ref2va")
    ap.add_argument("--dry-run", action="store_true",
                    help="只构建工作流并校验可序列化，不连 ComfyUI。**本机就能跑**")
    ap.add_argument("--n-refs", type=int, default=4,
                    help="用几张参考图。**0 = 对照臂**：不给任何参考图，只靠文本。"
                         "若 0 张与 6 张的 ArcFace 无差异，说明 ref_images 根本没被吃进去，"
                         "那是接线 bug，不是 H3 的能力结论")
    ap.add_argument("--tag", default="", help="产出文件名后缀，用于区分对照臂")
    args = ap.parse_args()

    refs = REF_SET[:max(0, args.n_refs)]
    length = snap_length(args.seconds) if args.seconds else args.length
    if (length - 5) % 17 != 0:
        print(f"[warn] length={length} 不在 17k+5 网格上，节点会自行上取整")
    os.makedirs(args.out, exist_ok=True)

    shots = json.load(open(args.shots_json, encoding="utf-8"))
    shots = shots["shots"] if isinstance(shots, dict) else shots
    by_id = {s["shot_id"]: s for s in shots}

    # 干跑不写 manifest：断点续跑靠它判定"哪些已完成"，混进没真跑过的记录会毒化续跑
    manifest = os.devnull if args.dry_run else os.path.join(args.out, "manifest.jsonl")
    results = []
    wanted = [x["shot_id"] for x in shots] if args.shots == "ALL" else args.shots.split(",")
    print(f"H3 Ref2VA · {len(wanted)} 镜头 · {args.width}x{args.height} · length={length}帧"
          f"(~{length/24:.1f}s) · {args.steps}步 · {len(refs)} 张参考图", flush=True)

    done = set()
    if not args.dry_run and os.path.exists(manifest):
        for line in open(manifest, encoding="utf-8"):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                      # 半行（崩在写入中途）直接跳过
            if r.get("status") == "ok" and r.get("tag") == args.tag:
                done.add(r["shot_id"])
        if done:
            print(f"断点续跑：已完成 {len(done)} 个，跳过", flush=True)

    with open(manifest, "a", encoding="utf-8") as log:
        for i, sid in enumerate(wanted, 1):
            s = by_id.get(sid)
            if not s:
                print(f"[{i}/{len(wanted)}] {sid} SKIP 不存在")
                continue
            if sid in done:
                continue
            prompt, _neg = l3_prompt.build(s)          # 与 L3 关键帧同一套提示词
            # 主体不在三位主角里（骆驼=末世小队男配、料理机器人=道具）就**不给参考图**。
            # 回退到主角参考集会把王天薇的脸糊到男配和机器人身上 —— 这三位之外没有角色圣经，
            # 宁可纯文本生成也不能锚错身份（红果要求同剧角色五官独立）。
            key = NAME2KEY.get(s.get("subject") or "")
            refs = REF_SETS[key][:max(0, args.n_refs)] if (key and args.n_refs) else []
            rec = {"shot_id": sid, "length": length, "size": [args.width, args.height],
                   "steps": args.steps, "seed": args.seed, "refs": len(refs),
                   "subject": s.get("subject"), "tag": args.tag}
            try:
                wf = build(prompt, args.width, args.height, length, args.steps,
                           args.seed, f"{sid}{args.tag}", refs)
                if args.dry_run:
                    blob = json.dumps({"prompt": wf}, ensure_ascii=False)
                    if "__" in blob:
                        raise ValueError("工作流仍含未替换的占位符")
                    rec.update(status="dry_ok", nodes=len(wf))
                else:
                    outs, secs = run(wf)
                    rec.update(status="ok" if outs else "no_output", outputs=outs, seconds=secs)
            except Exception as e:
                rec.update(status="failed", error=f"{type(e).__name__}: {e}"[:1200])
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            if not args.dry_run:
                os.fsync(log.fileno())
            results.append(rec)
            print(f"[{i}/{len(wanted)}] {sid} {rec['status']} ({rec.get('seconds','-')}s)", flush=True)
            if rec["status"] == "failed":
                print(f"      {rec['error'][:400]}", flush=True)

    ok = sum(1 for r in results if r["status"] in ("ok", "dry_ok"))
    tag = "干跑通过" if args.dry_run else "成功"
    print(f"\n{tag} {ok}/{len(results)}" + ("" if args.dry_run else f" -> {manifest}"))
    return 0 if ok == len(results) and results else 1


if __name__ == "__main__":
    sys.exit(main())
