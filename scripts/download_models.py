"""下载 4070 草稿工作流所需的 ComfyUI 模型（Wan2.1-T2V-1.3B）。

用法: comfy/venv/Scripts/python.exe scripts/download_models.py
来源: Comfy-Org/Wan_2.1_ComfyUI_repackaged（公开仓库，无需 token）
"""
import os
import sys

from huggingface_hub import hf_hub_download

REPO = "Comfy-Org/Wan_2.1_ComfyUI_repackaged"
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "comfy", "ComfyUI", "models")

# (repo 内路径, 本地 models 子目录)
FILES = [
    ("split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors", "diffusion_models"),
    ("split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", "text_encoders"),
    ("split_files/vae/wan_2.1_vae.safetensors", "vae"),
]

for repo_path, subdir in FILES:
    dest = os.path.join(ROOT, subdir)
    os.makedirs(dest, exist_ok=True)
    print(f"[download] {repo_path} -> {dest}", flush=True)
    path = hf_hub_download(repo_id=REPO, filename=repo_path, local_dir=dest)
    print(f"[done] {path}", flush=True)

print("ALL DONE", flush=True)
