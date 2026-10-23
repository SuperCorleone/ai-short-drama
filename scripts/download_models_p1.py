"""P1 代理站模型下载（4070 本机，8GB / 可用 ~5.8GB）。

用法:
    comfy/venv/Scripts/python.exe scripts/download_models_p1.py [zimage|anima|all]

设计:
  - Z-Image-Turbo 6B (Apache-2.0)  → 真人短剧代理关键帧（当前优先）
  - Anima 2B (非商用权重/产图可商用) → 漫剧代理关键帧
  - 下载后**扁平化**到 ComfyUI models/<类型>/，规避 hf_hub_download 保留 split_files/ 目录结构的坑
    （见 TASKS.md「主要收获」）
"""
import os
import shutil
import sys

from huggingface_hub import hf_hub_download

ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "comfy", "ComfyUI", "models",
)

# name -> [(repo_id, repo内路径, models子目录), ...]
GROUPS = {
    "zimage": [
        # bf16 主力（质量优先；ComfyUI DynamicVRAM 可在 8GB 卡上流式换入换出）
        ("Comfy-Org/z_image_turbo", "split_files/diffusion_models/z_image_turbo_bf16.safetensors", "diffusion_models"),
        # int8 提速备选
        ("Comfy-Org/z_image_turbo", "split_files/diffusion_models/z_image_turbo_int8_convrot.safetensors", "diffusion_models"),
        # Qwen3-4B 文本编码器 fp8（8GB 卡上唯一现实选择；bf16 版 8.04GB 太大）
        ("Comfy-Org/z_image_turbo", "split_files/text_encoders/qwen_3_4b_fp8_mixed.safetensors", "text_encoders"),
        ("Comfy-Org/z_image_turbo", "split_files/vae/ae.safetensors", "vae"),
    ],
    "anima": [
        ("circlestone-labs/Anima", "split_files/diffusion_models/anima-base-v1.0.safetensors", "diffusion_models"),
        ("circlestone-labs/Anima", "split_files/diffusion_models/anima-aesthetic-v1.1.safetensors", "diffusion_models"),
        ("circlestone-labs/Anima", "split_files/text_encoders/qwen_3_06b_base.safetensors", "text_encoders"),
        ("circlestone-labs/Anima", "split_files/vae/qwen_image_vae.safetensors", "vae"),
    ],
}


def fetch(repo_id: str, repo_path: str, subdir: str) -> None:
    dest_dir = os.path.join(ROOT, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    basename = os.path.basename(repo_path)
    final = os.path.join(dest_dir, basename)
    if os.path.exists(final):
        print(f"[skip] already present: {final}", flush=True)
        return
    print(f"[download] {repo_id} :: {repo_path}", flush=True)
    got = hf_hub_download(repo_id=repo_id, filename=repo_path, local_dir=dest_dir)
    # 扁平化: dest_dir/split_files/<type>/<file> -> dest_dir/<file>
    if os.path.abspath(got) != os.path.abspath(final):
        shutil.move(got, final)
        nested = os.path.join(dest_dir, "split_files")
        if os.path.isdir(nested):
            shutil.rmtree(nested, ignore_errors=True)
    size = os.path.getsize(final) / 1e9
    print(f"[done] {final}  ({size:.2f} GB)", flush=True)


def main() -> None:
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    groups = list(GROUPS) if which == "all" else [which]
    for g in groups:
        if g not in GROUPS:
            print(f"[error] unknown group: {g}; choose from {list(GROUPS)} or 'all'")
            sys.exit(1)
    for g in groups:
        print(f"\n===== group: {g} =====", flush=True)
        for repo_id, repo_path, subdir in GROUPS[g]:
            fetch(repo_id, repo_path, subdir)
    print("\nALL DONE", flush=True)


if __name__ == "__main__":
    main()
