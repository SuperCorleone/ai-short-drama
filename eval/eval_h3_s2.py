"""S2 · MiniMax-H3 Ref2VA 四镜头门禁判定。

判定标准在 TASKS.md §2 L4 已**事先写死**（否则会变成"看着还行就继续"）：
  ① ArcFace 按分档门禁达标
  ② 目检无穿模 / 无身份漂移
  ③ RAFT 光流落在 0.2–2.0（下界防抖音明禁的静态 PPT，上界防糊）

⚠️ RAFT 的抽帧口径问题（与 clip_t 换模型作废旧阈值是同一类）：
`_read_frames` 在全片均匀抽 max_frames 帧，RAFT 算的是**相邻抽样帧**之间的光流
=> 幅值直接随抽帧间隔变化。0.2–2.0 这个阈值定的时候没有记录抽帧协议，
M0 的基线 0.6317 是在 2s/16fps/12帧（间隔≈0.19s）下测的。
本片是 5.17s/24fps，用默认 16 帧会变成间隔 0.34s——**尺度不同，不可直接比**。
故两种口径都报：`matched` 对齐 M0 的时间间隔，`default` 为服务默认值。
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8391"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VID_DIR = os.path.join(ROOT, "pipeline", "shots", "h3_s2")
SHOTS_JSON = os.path.join(ROOT, "pipeline", "previs", "shots.json")
REF_FACE = os.path.join(ROOT, "comfy", "ComfyUI", "output", "bible",
                        "wang_tianwei_s1001_00001_.png")
SHOTS = ["G01-S05", "G01-S03", "G04-S05", "G05-S01"]

M0_GAP_S = 3 / 16.0          # M0 基线的抽样时间间隔（33帧/2s，抽12帧 => 步长3）
FPS = 24


def post(path, payload, timeout=900):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_health(limit=300):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=10) as r:
                return r.read().decode("utf-8")
        except Exception:
            time.sleep(5)
    raise SystemExit("[FATAL] L1 服务 5 分钟未就绪")


def main():
    print("[health]", wait_health(), flush=True)
    by_id = {s["shot_id"]: s for s in json.load(open(SHOTS_JSON, encoding="utf-8"))["shots"]}

    rows = []
    for sid in SHOTS:
        vid = os.path.join(VID_DIR, f"{sid}_00001_.mp4")
        if not os.path.exists(vid):
            print(f"[SKIP] {sid} 无产物")
            continue
        s = by_id[sid]
        # CLIP-T 只喂正向画面描述：生成时那句「避免：…」是负面词，
        # 拼进文本嵌入纯属噪声（它描述的是不该出现的东西）
        parts = [s.get("scene", ""), s.get("action") or "",
                 s.get("shot_size") or "", s.get("camera_move") or ""]
        prompt = "，".join(p for p in parts if p)

        n = 124
        mf_matched = max(2, round(n / (M0_GAP_S * FPS)))   # 对齐 M0 的时间间隔
        r = post("/eval", {"video": vid, "prompt": prompt, "ref_face": REF_FACE,
                           "max_frames": mf_matched})
        r2 = post("/eval", {"video": vid, "prompt": prompt, "ref_face": REF_FACE,
                            "max_frames": 16})
        rows.append({
            "shot_id": sid, "shot_size": s.get("shot_size"), "prompt": prompt[:60],
            "clip_t": r.get("clip_t"), "dinov2": r.get("dinov2"),
            "arcface": r.get("arcface"), "musiq": r.get("musiq"),
            "raft_matched": r.get("raft_flow"), "raft_default": r2.get("raft_flow"),
            "mf_matched": mf_matched,
        })
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    out = os.path.join(ROOT, "pipeline", "shots", "h3_s2", "l1_report.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n-> {out}")

    print("\n=== 门禁判定 ===")
    print(f"{'镜头':<10}{'景别':<8}{'arcface':>9}{'raft(M0口径)':>14}{'musiq':>8}{'clip_t':>9}")
    for r in rows:
        print(f"{r['shot_id']:<10}{str(r['shot_size']):<8}"
              f"{str(r['arcface']):>9}{str(r['raft_matched']):>14}"
              f"{str(r['musiq']):>8}{str(r['clip_t']):>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
