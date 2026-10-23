"""TTS 服务实测脚本：健康检查 → 两条分镜台词 → 保存结果摘要（UTF-8 安全）。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8390"
LINES = [
    "主人，请问有什么需要我做的吗？",
    "好的，如果您有需要，请随时叫我。",
]


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


if __name__ == "__main__":
    print("[health]", get("/health"))
    results = []
    for line in LINES:
        t0 = time.time()
        res = post("/tts", {"text": line})
        res["text"] = line
        res["wall"] = round(time.time() - t0, 2)
        print("[tts]", json.dumps(res, ensure_ascii=False))
        results.append(res)
    with open("tts/test_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("OK")
