"""P1.3b 角色圣经 v0 —— 基准脸候选生成（4070 本机 · Z-Image-Turbo）。

按 PLAN_v2 架构定律 2：基准脸的「挑选」是创作决策，必须在本机代理上完成；
下游的三视图/表情集/服装表派生（Qwen-Image-Edit-2511，20B）本地跑不下，交 A100。

流程:
  1) 每个角色用 N 个随机种子出 N 张基准脸候选（正面/中性/柔光/纯背景）
  2) 用 L1 服务 /faces 做两两 ArcFace：
       跨角色 ≤ 0.65（红果红线）  同角色一致性仅作参考（候选本就该有差异）
  3) 输出 candidates.json，供人工挑选后写回 characters.json 的 locked_face 字段

用法:
  # 阶段一（需 ComfyUI :8189 在跑，eval 服务须停以让出显存）
  comfy/venv/Scripts/python.exe pipeline/assets/build_character_bible.py gen --n 6
  # 阶段二（需 L1 eval :8391 在跑，ComfyUI 须停）
  eval/venv/Scripts/python.exe pipeline/assets/build_character_bible.py gate
"""
import argparse
import itertools
import json
import os
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPEC = os.path.join(HERE, "characters.json")
CAND_DIR = os.path.join(HERE, "candidates")
CAND_JSON = os.path.join(HERE, "candidates.json")
COMFY = "http://127.0.0.1:8189"
EVAL = "http://127.0.0.1:8391"
WORKFLOW = os.path.join(ROOT, "pipeline", "keyframe", "zimage_workflow.json")
COMFY_OUTPUT = os.path.join(ROOT, "comfy", "ComfyUI", "output")

# 基准脸用方形而非 9:16：人脸检测与 ArcFace 在方图上最稳，
# 且它只是参考资产，不是成片帧。
FACE_W, FACE_H = 1024, 1024

BASE_NEGATIVE = (
    "lowres, blurry, jpeg artifacts, watermark, text, extra fingers, deformed hands, "
    "bad anatomy, oversaturated, plastic skin, cgi look, flat lighting, "
    "multiple people, cropped face, side view, closed eyes"
)


def post(url, payload, timeout=900):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def gen(args):
    spec = json.load(open(SPEC, encoding="utf-8"))
    os.makedirs(CAND_DIR, exist_ok=True)
    wf_template = json.load(open(WORKFLOW, encoding="utf-8"))

    records = []
    for ch in spec["characters"]:
        neg = BASE_NEGATIVE + (", " + ch["negative_extra"] if ch.get("negative_extra") else "")
        for k in range(args.n):
            seed = args.seed_base + k
            wf = json.loads(json.dumps(wf_template))
            wf["4"]["inputs"]["text"] = ch["baseline_prompt"]
            wf["5"]["inputs"]["text"] = neg
            wf["6"]["inputs"]["width"] = FACE_W
            wf["6"]["inputs"]["height"] = FACE_H
            wf["7"]["inputs"]["steps"] = args.steps
            wf["7"]["inputs"]["cfg"] = args.cfg
            wf["7"]["inputs"]["seed"] = seed
            wf["9"]["inputs"]["filename_prefix"] = f"bible/{ch['id']}_s{seed}"

            t0 = time.time()
            rec = {"character": ch["id"], "name": ch["name"], "seed": seed}
            try:
                pid = post(COMFY + "/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})["prompt_id"]
                imgs = []
                t1 = time.time()
                while time.time() - t1 < 900:
                    hist = get(f"{COMFY}/history/{pid}")
                    if pid in hist:
                        st = hist[pid].get("status", {})
                        if st.get("status_str") == "error":
                            raise RuntimeError(json.dumps(st.get("messages", []), ensure_ascii=False)[:400])
                        for out in hist[pid].get("outputs", {}).values():
                            for im in out.get("images", []):
                                imgs.append(os.path.join(COMFY_OUTPUT, im.get("subfolder", ""), im["filename"]))
                        break
                    time.sleep(1.0)
                if not imgs:
                    raise RuntimeError("无输出")
                rec["path"] = imgs[0]
                rec["status"] = "ok"
            except Exception as e:
                rec["status"] = "failed"
                rec["error"] = str(e)[:300]
            rec["seconds"] = round(time.time() - t0, 2)
            records.append(rec)
            print(f"[{len(records)}] {ch['id']} seed={seed} {rec['status']} ({rec['seconds']}s)", flush=True)

    json.dump(records, open(CAND_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    ok = sum(1 for r in records if r["status"] == "ok")
    print(f"\n{ok}/{len(records)} 成功 → {CAND_JSON}")


def gate(args):
    """对每个角色的候选做人脸检出，再做跨角色两两相似度（红果红线）。"""
    records = [r for r in json.load(open(CAND_JSON, encoding="utf-8")) if r["status"] == "ok"]
    if not records:
        print("[error] 没有可用候选，先跑 gen")
        sys.exit(1)

    items = [{"path": r["path"], "role": r["character"]} for r in records]
    res = post(EVAL + "/faces", {"items": items, "cross_role_max": args.cross_max,
                                 "same_role_min": 0.0})  # 同角色候选本就该有差异，此处不设下限

    by_path = {f["path"]: f for f in res["faces"]}
    print("=== 人脸检出 ===")
    for r in records:
        f = by_path.get(r["path"], {})
        print(f"  {r['character']:<14} seed={r['seed']:<5} "
              f"{'OK ' if f.get('found') else 'MISS'}  det={f.get('det_score')}")

    cross = [p for p in res["pairs"] if not p["same_role"]]
    print(f"\n=== 跨角色相似度（红果红线 <= {args.cross_max}）===")
    print(f"  跨角色对数: {len(cross)}   最大值: {res['cross_role_max']}")
    for p in cross[:8]:
        flag = "OVER" if p["cos"] > args.cross_max else "ok"
        print(f"  {p['cos']:.4f} {flag:<5} {p['role_a']} x {p['role_b']}  "
              f"({os.path.basename(p['a'])} / {os.path.basename(p['b'])})")

    # 同角色内部离散度：候选之间差异越大，说明可选空间越宽
    same = [p for p in res["pairs"] if p["same_role"]]
    if same:
        import statistics
        by_role = {}
        for p in same:
            by_role.setdefault(p["role_a"], []).append(p["cos"])
        print("\n=== 同角色候选内部相似度（越低=候选差异越大，可选空间越宽）===")
        for role, vals in by_role.items():
            print(f"  {role:<14} 均值 {statistics.mean(vals):.4f}  "
                  f"范围 [{min(vals):.4f}, {max(vals):.4f}]  n={len(vals)}")

    missing = res["faces_not_detected"]
    verdict = (res["cross_role_max"] is None or res["cross_role_max"] <= args.cross_max) and not missing
    print(f"\n未检出人脸: {len(missing)}/{len(items)}")
    print(f"\n判定: {'PASS — 可进入挑选锁定' if verdict else 'FAIL — 需重出候选或调整角色设定'}")

    out = os.path.join(HERE, "gate_report.json")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"完整报告 → {out}")
    sys.exit(0 if verdict else 1)


def select(args):
    """在候选里穷举每角色选 1 张的组合，找跨角色最大相似度最小的那一组。

    门禁 FAIL 不等于要重出：候选之间差异很大，通常存在一个满足红线的组合。
    先搜索，搜不到再回去改角色设定。
    """
    res = json.load(open(os.path.join(HERE, "gate_report.json"), encoding="utf-8"))
    cos = {}
    for p in res["pairs"]:
        cos[(p["a"], p["b"])] = p["cos"]
        cos[(p["b"], p["a"])] = p["cos"]

    by_role = {}
    for f in res["faces"]:
        if f["found"]:
            by_role.setdefault(f["role"], []).append(f["path"])
    roles = sorted(by_role)
    if len(roles) < 2:
        print("[error] 角色数不足 2，无需组合搜索")
        sys.exit(1)

    best, best_max = None, 2.0
    for combo in itertools.product(*(by_role[r] for r in roles)):
        m = max(cos.get((a, b), 0.0) for a, b in itertools.combinations(combo, 2))
        if m < best_max:
            best_max, best = m, combo

    ok = best_max <= args.cross_max
    print(f"搜索空间: {' x '.join(str(len(by_role[r])) for r in roles)} = "
          f"{len(list(itertools.product(*(by_role[r] for r in roles))))} 组\n")
    print(f"最优组合的跨角色最大相似度: {best_max:.4f}  (红线 <= {args.cross_max})  "
          f"-> {'PASS' if ok else 'FAIL'}\n")
    for role, path in zip(roles, best):
        print(f"  {role:<14} {os.path.basename(path)}")
    print("\n该组合内两两相似度:")
    for (ra, a), (rb, b) in itertools.combinations(zip(roles, best), 2):
        c = cos.get((a, b), 0.0)
        print(f"  {c:.4f} {'OVER' if c > args.cross_max else 'ok  '}  {ra} x {rb}")

    out = {
        "selected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cross_role_max": round(best_max, 4),
        "threshold": args.cross_max,
        "pass": ok,
        "locked": {role: path for role, path in zip(roles, best)},
    }
    dest = os.path.join(HERE, "locked_faces.json")
    json.dump(out, open(dest, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n-> {dest}")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--cross-max", type=float, default=0.65)
    s.set_defaults(func=select)
    g = sub.add_parser("gen")
    g.add_argument("--n", type=int, default=6, help="每角色候选数")
    g.add_argument("--seed-base", type=int, default=1000)
    g.add_argument("--steps", type=int, default=8)
    g.add_argument("--cfg", type=float, default=1.0)
    g.set_defaults(func=gen)
    t = sub.add_parser("gate")
    t.add_argument("--cross-max", type=float, default=0.65, help="红果跨角色相似度红线")
    t.set_defaults(func=gate)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
