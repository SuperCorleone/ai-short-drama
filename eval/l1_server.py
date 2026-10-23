"""L1 硬指标评估服务（4070 轻量常驻卡 · 评估位）。

五项硬指标（对齐 VBench 协议，串行加载，合计 <6GB）：
  clip_t   — CLIP 文-视频对齐（open_clip ViT-B-32，逐帧与 prompt 余弦，取均值）
  dinov2   — DINOv2 主体一致性（相邻帧 embedding 余弦均值）
  arcface  — InsightFace buffalo_l 角色身份相似度（参考人脸 ↔ 帧内最大人脸）
  raft     — RAFT 光流动态程度（相邻帧平均光流幅值，抽样计算）
  musiq    — MUSIQ 成像质量（pyiqa，逐帧均值）

用法:
  eval/venv/Scripts/python.exe eval/l1_server.py        # 监听 127.0.0.1:8391

  POST /eval    视频评估（五指标）
    {"video": "abs/path.mp4", "prompt": "...", "ref_face": "abs/path.png"|null, "max_frames": 16}
    -> {"clip_t":.., "dinov2":.., "arcface":..|null, "raft_flow":.., "musiq":..,
        "seconds":.., "frames_used":..}

  POST /eval_image   单张关键帧评估（P1 代理关键帧用；无 raft/dinov2 时序项）
    {"image": "abs/path.png", "prompt": "...", "ref_face": "abs/path.png"|null}
    -> {"clip_t":.., "musiq":.., "arcface":..|null, "seconds":..}

  POST /faces   ★角色人脸门禁（P1.3，对齐红果 AI 人脸相似度比对系统）
    {"items": [{"path":"abs/a.png","role":"女主"}, ...],
     "cross_role_max": 0.65, "same_role_min": 0.75}
    -> 两两 ArcFace 余弦矩阵 + 门禁判定
       · 跨角色最大相似度 ≤ 0.65  （红果红线：>65% 自动驳回）
       · 同角色最小相似度 ≥ 0.75  （长片身份漂移控制）

注意: buffalo_l 为非商业研究许可，产品化须替换授权（TASKS.md 长期纪律）。
"""
import os
import time

import numpy as np
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="ai-short-drama L1 eval", version="0.1.0")
_models = {}

MAX_DIM = 384  # 帧降采样边长，控制 RAFT/编码耗时与显存


def _load(name):
    if name in _models:
        return _models[name]
    import torch

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if name == "clip":
        import open_clip

        # ⚠️ 2026-08-08 换模型，原因见下（这是个静默了很久的坏指标）：
        # 原用 ViT-B-32 / laion2b_s34b_b79k —— 那是**英文模型**。实测同一张图：
        #     中文-切题 0.1933 / 中文-完全不相关 0.2017 ← 切题的反而更低
        #     英文-切题 0.3818 / 英文-完全不相关 -0.0378
        # 即中文下 clip_t **完全没有鉴别力，是纯噪声**。而本项目整条管线都用中文提示词，
        # 意味着 clip_t 自 T1.2 起一直无效，M0 记录的 0.4042 也不具参考价值。
        # 改用多语言 XLM-R 版本；CLIP_MODEL 环境变量可切回旧模型做对照。
        spec = os.environ.get("CLIP_MODEL", "xlm-roberta-base-ViT-B-32:laion5b_s13b_b90k")
        arch, pretrained = spec.split(":", 1)
        m, _, pp = open_clip.create_model_and_transforms(arch, pretrained=pretrained)
        tok = open_clip.get_tokenizer(arch)
        obj = (m.eval().to(dev), pp, tok)
    elif name == "dinov2":
        m = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14")
        obj = m.eval().to(dev)
    elif name == "arcface":
        from insightface.app import FaceAnalysis

        fa = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        fa.prepare(ctx_id=0, det_size=(320, 320))
        obj = fa
    elif name == "raft":
        from torchvision.models.optical_flow import raft_large, Raft_Large_Weights

        m = raft_large(weights=Raft_Large_Weights.DEFAULT)
        obj = m.eval().to(dev)
    elif name == "musiq":
        import pyiqa

        obj = pyiqa.create_metric("musiq", device=dev)
    else:
        raise KeyError(name)
    _models[name] = obj
    return obj


def _read_frames(video, max_frames):
    import cv2

    cap = cv2.VideoCapture(video)
    frames = []
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    idxs = np.linspace(0, max(n - 1, 0), min(max_frames, n)).astype(int)
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, fr = cap.read()
        if not ok:
            continue
        h, w = fr.shape[:2]
        if max(h, w) > MAX_DIM:
            s = MAX_DIM / max(h, w)
            fr = cv2.resize(fr, (int(w * s), int(h * s)))
        frames.append(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
    cap.release()
    if len(frames) < 2:
        raise ValueError(f"可读帧不足: {len(frames)}")
    return frames


def _to_tensor(frames):
    import torch

    x = torch.from_numpy(np.stack(frames)).permute(0, 3, 1, 2).float() / 255.0
    return x


def eval_clip_t(frames, prompt, dev="cuda"):
    import torch
    from PIL import Image

    m, pp, tok = _load("clip")
    with torch.no_grad():
        imgs = torch.stack([pp(Image.fromarray(f)) for f in frames]).to(dev)
        txt = tok([prompt]).to(dev)
        fi = m.encode_image(imgs)
        ft = m.encode_text(txt)
        fi = fi / fi.norm(dim=-1, keepdim=True)
        ft = ft / ft.norm(dim=-1, keepdim=True)
        return float((fi @ ft.T).mean().item())


def eval_dinov2(frames, dev="cuda"):
    import torch
    import torch.nn.functional as F

    m = _load("dinov2")
    x = _to_tensor(frames).to(dev)
    mean = torch.tensor([0.485, 0.456, 0.406], device=dev).view(1, 3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225], device=dev).view(1, 3, 1, 1)
    x = F.interpolate((x - mean) / std, size=(224, 224), mode="bilinear", align_corners=False)
    with torch.no_grad():
        e = m(x)
        e = e / e.norm(dim=-1, keepdim=True)
        sims = (e[:-1] * e[1:]).sum(-1)
        return float(sims.mean().item())


def eval_arcface(frames, ref_face_path):
    if not ref_face_path or not os.path.exists(ref_face_path):
        return None
    import cv2

    fa = _load("arcface")
    ref = cv2.imread(ref_face_path)
    ref_faces = fa.get(ref)
    if not ref_faces:
        return None
    ref_emb = ref_faces[0].normed_embedding
    sims = []
    for f in frames:
        bgr = cv2.cvtColor(f, cv2.COLOR_RGB2BGR)
        faces = fa.get(bgr)
        if not faces:
            continue
        best = max(faces, key=lambda x: x.bbox[2] * x.bbox[3])
        sims.append(float(np.dot(ref_emb, best.normed_embedding)))
    return float(np.mean(sims)) if sims else None


def eval_raft(frames, dev="cuda"):
    import torch
    import torch.nn.functional as F

    m = _load("raft")
    x = _to_tensor(frames).to(dev)
    x = (x * 2.0 - 1.0)  # RAFT 期望 [-1, 1]
    h, w = x.shape[-2:]
    x = F.interpolate(x, size=(h - h % 8, w - w % 8), mode="bilinear", align_corners=False)  # RAFT 要求 H/W 可被 8 整除
    mags = []
    with torch.no_grad():
        for i in range(len(frames) - 1):
            f1 = x[i : i + 1]
            f2 = x[i + 1 : i + 2]
            flows = m(f1, f2)[-1]
            mags.append(float(flows.norm(dim=1).mean().item()))
    return float(np.mean(mags))


def eval_musiq(frames, dev="cuda"):
    import torch

    metric = _load("musiq")
    x = _to_tensor(frames).to(dev)
    with torch.no_grad():
        return float(metric(x).mean().item())


def _read_image(path):
    """读单张图 → RGB ndarray（同 _read_frames 的降采样策略，保证指标可比）。"""
    import cv2

    if not os.path.exists(path):
        raise FileNotFoundError(path)
    fr = cv2.imread(path)
    if fr is None:
        raise ValueError(f"无法解码图像: {path}")
    h, w = fr.shape[:2]
    if max(h, w) > MAX_DIM:
        s = MAX_DIM / max(h, w)
        fr = cv2.resize(fr, (int(w * s), int(h * s)))
    return cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)


def _face_embedding(path):
    """取图中最大人脸的归一化 embedding。返回 (emb|None, det_score|None)。

    人脸检测走原图（不降采样），避免小脸漏检。
    """
    import cv2

    fa = _load("arcface")
    bgr = cv2.imread(path)
    if bgr is None:
        return None, None
    faces = fa.get(bgr)
    if not faces:
        return None, None
    best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    return best.normed_embedding, float(best.det_score)


class EvalReq(BaseModel):
    video: str
    prompt: str = ""
    ref_face: str | None = None
    max_frames: int = 16


class EvalImageReq(BaseModel):
    image: str
    prompt: str = ""
    ref_face: str | None = None


class FaceItem(BaseModel):
    path: str
    role: str = ""


class FacesReq(BaseModel):
    items: list[FaceItem]
    cross_role_max: float = 0.65  # 红果红线：跨角色相似度 > 65% 自动驳回
    same_role_min: float = 0.75   # 同角色身份保持下限


@app.get("/health")
def health():
    return {"ok": True, "loaded": list(_models)}


@app.post("/eval")
def evaluate(req: EvalReq):
    t0 = time.time()
    frames = _read_frames(req.video, req.max_frames)
    out = {"frames_used": len(frames)}
    if req.prompt:
        out["clip_t"] = round(eval_clip_t(frames, req.prompt), 4)
    out["dinov2"] = round(eval_dinov2(frames), 4)
    af = eval_arcface(frames, req.ref_face)
    out["arcface"] = round(af, 4) if af is not None else None
    out["raft_flow"] = round(eval_raft(frames), 4)
    out["musiq"] = round(eval_musiq(frames), 2)
    out["seconds"] = round(time.time() - t0, 2)
    return out


@app.post("/eval_image")
def evaluate_image(req: EvalImageReq):
    """单张关键帧评估：clip_t + musiq（+ arcface）。无时序指标。"""
    t0 = time.time()
    frame = _read_image(req.image)
    out = {}
    if req.prompt:
        out["clip_t"] = round(eval_clip_t([frame], req.prompt), 4)
    out["musiq"] = round(eval_musiq([frame]), 2)
    af = eval_arcface([frame], req.ref_face)
    out["arcface"] = round(af, 4) if af is not None else None
    out["seconds"] = round(time.time() - t0, 2)
    return out


@app.post("/faces")
def faces(req: FacesReq):
    """角色人脸门禁：两两 ArcFace 余弦 + 红果/一致性双向判定。"""
    t0 = time.time()
    embs, meta = [], []
    for it in req.items:
        emb, score = _face_embedding(it.path)
        meta.append({
            "path": it.path,
            "role": it.role,
            "found": emb is not None,
            "det_score": round(score, 4) if score is not None else None,
        })
        embs.append(emb)

    pairs = []
    cross_vals, same_vals = [], []
    for i in range(len(embs)):
        for j in range(i + 1, len(embs)):
            if embs[i] is None or embs[j] is None:
                continue
            cos = float(np.dot(embs[i], embs[j]))
            same_role = bool(req.items[i].role) and req.items[i].role == req.items[j].role
            pairs.append({
                "a": req.items[i].path, "b": req.items[j].path,
                "role_a": req.items[i].role, "role_b": req.items[j].role,
                "same_role": same_role, "cos": round(cos, 4),
            })
            (same_vals if same_role else cross_vals).append(cos)

    cross_max = max(cross_vals) if cross_vals else None
    same_min = min(same_vals) if same_vals else None
    missing = [m["path"] for m in meta if not m["found"]]

    gate = {
        # 红果红线：任意跨角色对的相似度都必须 ≤ cross_role_max
        "cross_role_ok": (cross_max is None) or (cross_max <= req.cross_role_max),
        # 同角色身份保持：所有同角色对必须 ≥ same_role_min
        "same_role_ok": (same_min is None) or (same_min >= req.same_role_min),
        "all_faces_detected": not missing,
    }
    gate["pass"] = all(gate.values())

    # 违规明细，便于直接定位要重做哪张脸
    violations = [p for p in pairs
                  if (not p["same_role"] and p["cos"] > req.cross_role_max)
                  or (p["same_role"] and p["cos"] < req.same_role_min)]

    return {
        "faces": meta,
        "pairs": sorted(pairs, key=lambda p: -p["cos"]),
        "cross_role_max": round(cross_max, 4) if cross_max is not None else None,
        "same_role_min": round(same_min, 4) if same_min is not None else None,
        "thresholds": {"cross_role_max": req.cross_role_max, "same_role_min": req.same_role_min},
        "gate": gate,
        "violations": violations,
        "faces_not_detected": missing,
        "seconds": round(time.time() - t0, 2),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8391, log_level="warning")
