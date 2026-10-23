"""F5-TTS HTTP 服务（4070 轻量常驻卡 · TTS 配音位）。

用法:
  tts/venv/Scripts/python.exe tts/server.py          # 监听 127.0.0.1:8390
  POST /tts  {"text": "...", "ref_audio": null, "ref_text": null, "speed": 1.0}
  -> {"wav": "<abs path>", "seconds": 1.23, "sr": 24000}

注意: F5-TTS 模型许可为 cc-by-nc-4.0（非商用），仅用于研发测试；
      产品化须替换为 CosyVoice2(Apache-2.0) 等商用许可模型（见 TASKS.md T1.2）。
"""
import os
import time
import uuid

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

# torchaudio 2.8 默认走 torchcodec（需 FFmpeg shared DLL，本机只有静态 ffmpeg.exe）；
# F5-TTS 仅用它读参考音频（f5_tts/infer/utils_infer.py:403），用 soundfile 实现替换。
import soundfile as sf
import torch
import torchaudio


def _sf_load(path, *args, **kwargs):
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(data.T.copy()), sr


torchaudio.load = _sf_load

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

DEFAULT_REF = os.path.join(ROOT, "ref", "basic_ref_en.wav")
# 英文参考音频文本已知（F5-TTS 官方示例）；ref_text="" 会走 transformers Whisper 管线，
# 它内部用 torchcodec 读音频——本机无 FFmpeg shared DLL 必炸，故禁用自动转写路径。
# 后续接入自制中文参考音频时，必须同时提供其文本。
DEFAULT_REF_TEXT = "Some call me nature, others call me mother nature."

app = FastAPI(title="ai-short-drama tts", version="0.1.0")
_model = None


def get_model():
    global _model
    if _model is None:
        from f5_tts.api import F5TTS

        _model = F5TTS(model="F5TTS_v1_Base", device="cuda")
    return _model


class TTSReq(BaseModel):
    text: str
    ref_audio: str | None = None
    ref_text: str | None = None
    speed: float = 1.0
    nfe_step: int = 32


@app.get("/health")
def health():
    return {"ok": True, "model_loaded": _model is not None}


@app.post("/tts")
def tts(req: TTSReq):
    t0 = time.time()
    m = get_model()
    out = os.path.join(OUT, f"{uuid.uuid4().hex[:12]}.wav")
    wav, sr, _ = m.infer(
        ref_file=req.ref_audio or DEFAULT_REF,
        ref_text=DEFAULT_REF_TEXT if req.ref_text is None else req.ref_text,
        gen_text=req.text,
        speed=req.speed,
        nfe_step=req.nfe_step,
        file_wave=out,
    )
    # `seconds` 曾经是**生成耗时**，字段名却读起来像音频时长 —— 下游 animatic 的
    # 节奏报告一度拿它当台词时长用，把一句 7 个字的台词算成 76 秒（那其实是冷启耗时）。
    # 现在两者都返回并各自命名清楚；`seconds` 保留旧语义以免破坏既有调用方。
    n = wav.shape[-1] if hasattr(wav, "shape") else len(wav)
    return {"wav": out,
            "seconds": round(time.time() - t0, 2),      # 生成耗时（向后兼容）
            "gen_seconds": round(time.time() - t0, 2),  # 同上，名字不含歧义
            "duration_s": round(n / float(sr), 3),      # **音频时长**
            "sr": sr}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8390, log_level="warning")
