"""P1.3 冒烟：/faces 角色人脸门禁 与 /eval_image 单帧评估。

用法: eval/venv/Scripts/python.exe eval/test_faces.py [图片目录]

不传目录时用 pipeline/previs/frames（灰盒预演帧，无人脸）验证"无脸时的正确降级行为"。
一律用 urllib + 显式 UTF-8（Git Bash 下 curl 会把中文 JSON 转 GBK，见 TASKS.md）。
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8391"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def post(path, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_health(timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(2)
    raise RuntimeError("l1_server 未就绪")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "pipeline", "previs", "frames")
    print("health:", wait_health())

    imgs = sorted(
        os.path.join(src, f) for f in os.listdir(src) if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )[:6]
    if not imgs:
        print(f"[error] {src} 下没有图片")
        sys.exit(1)
    print(f"样本 {len(imgs)} 张，来自 {src}\n")

    # --- /eval_image ---
    t0 = time.time()
    r1 = post("/eval_image", {"image": imgs[0], "prompt": "厨房内景，中景镜头"})
    print("[eval_image] 冷启:", json.dumps(r1, ensure_ascii=False))
    r2 = post("/eval_image", {"image": imgs[1], "prompt": "厨房内景，中景镜头"})
    print("[eval_image] 热跑:", json.dumps(r2, ensure_ascii=False))

    # --- /faces ---
    # 交替分配两个角色标签，构造既有同角色对也有跨角色对的最小用例
    items = [{"path": p, "role": "女主" if i % 2 == 0 else "男主"} for i, p in enumerate(imgs)]
    rf = post("/faces", {"items": items})
    print("\n[faces] gate:", json.dumps(rf["gate"], ensure_ascii=False))
    print("[faces] cross_role_max:", rf["cross_role_max"], " same_role_min:", rf["same_role_min"])
    print("[faces] 未检出人脸:", len(rf["faces_not_detected"]), "/", len(items))
    print("[faces] 违规对:", len(rf["violations"]), " 耗时:", rf["seconds"], "s")

    detected = [f for f in rf["faces"] if f["found"]]
    if not detected:
        # 灰盒帧没有人脸是预期的：门禁必须因 all_faces_detected=False 而不通过，
        # 而不是因为"没有可比的对"就误判为通过。
        ok = rf["gate"]["all_faces_detected"] is False and rf["gate"]["pass"] is False
        print(f"\n[{'PASS' if ok else 'FAIL'}] 无人脸时正确降级（不误判为通过）")
        sys.exit(0 if ok else 1)

    print(f"\n[INFO] 检出 {len(detected)} 张脸，门禁判定 pass={rf['gate']['pass']}")
    for p in rf["pairs"][:5]:
        print(f"  {p['cos']:.4f}  {'同角色' if p['same_role'] else '跨角色'}  "
              f"{os.path.basename(p['a'])} ↔ {os.path.basename(p['b'])}")
    print(f"总耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
