"""L1 评估服务实测：用 pipeline/draft/wan_t2v_shot08.mp4 跑五指标（UTF-8 安全）。"""
import json
import os
import time
import urllib.request

BASE = "http://127.0.0.1:8391"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO = os.path.join(ROOT, "pipeline", "draft", "wan_t2v_shot08.mp4")
PROMPT = "A girl opens a big fridge full of fresh fruits and vegetables in a dark high-tech kitchen at night, warm light spills out"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


if __name__ == "__main__":
    with urllib.request.urlopen(BASE + "/health", timeout=30) as r:
        print("[health]", r.read().decode("utf-8"))
    t0 = time.time()
    res = post("/eval", {"video": VIDEO, "prompt": PROMPT, "max_frames": 12})
    res["wall"] = round(time.time() - t0, 2)
    print("[eval]", json.dumps(res, ensure_ascii=False))
    with open(os.path.join(ROOT, "eval", "test_result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("OK")
