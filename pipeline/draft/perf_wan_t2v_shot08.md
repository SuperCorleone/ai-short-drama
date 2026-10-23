# Wan2.1 T2V 1.3B 草稿生成性能记录（2026-07-23）

硬件：RTX 4070 Laptop 8GB（测试时桌面进程已占 ~2.3GB，可用 ~5.8GB）/ 32GB RAM
软件：ComfyUI 0.28.0，torch 2.11.0+cu128，GPU 模式 NORMAL_VRAM（DynamicVRAM 自动 offload）
工作流：pipeline/draft/wan_t2v_13b_workflow.json（832×480，33 帧，fps 16，steps 10，cfg 5.0，uni_pc/simple）

## 耗时

| 档位 | 范围 | 耗时 |
|---|---|---|
| 冷启动档（首次，含 umt5 6.7GB + unet 2.8GB + VAE 加载） | submit→success | 66.6 s |
| 热跑档（模型已在内存/显存，换 seed 重跑） | submit→success | 55.4 s |

33 帧档远低于 10 分钟阈值，未再测 17 帧档。

## 显存

- 峰值：7685 MiB / 8188 MiB（nvidia-smi 每 2s 采样，原始数据 vram_33f.log）
- 未 OOM，未启用 --lowvram / --reserve-vram（默认策略即可）
- umt5 fp8（6419MB）由 DynamicVRAM 动态加载，offload 到内存
- 结束后显存回落到 ~1.1GB

## 产出

- pipeline/draft/wan_t2v_shot08.mp4（h264，832×480，16fps，33 帧，2.06s，150KB）
- pipeline/draft/wan_t2v_13b_workflow.json（可复用 API 格式工作流）
- 原始记录：history_33f.json / history_warm.json / vram_33f.log / frame_first.png / frame_mid.png
