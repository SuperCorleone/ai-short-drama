# Z-Image-Turbo @ 4070 实测（P1.1）

日期：2026-08-04 · 机器：RTX 4070 Laptop 8GB（桌面占约 2.0GB）· ComfyUI 0.28.0 @HEAD `2e47082`

## 配置

| 项 | 值 |
|---|---|
| 扩散模型 | `z_image_turbo_bf16.safetensors`（12.31 GB，bf16 全精度） |
| 文本编码器 | `qwen_3_4b_fp8_mixed.safetensors`（5.63 GB） |
| VAE | `ae.safetensors`（0.34 GB） |
| 架构 | ZImage extends Lumina2 —— 16 通道 Flux latent，`shift=3.0` 内置 |
| CLIPLoader type | `stable_diffusion`（**不是** flux/flux2；ComfyUI 按 TE 模型 `QWEN3_4B` 自动路由到 z_image 分支，见 `comfy/sd.py:1678`） |
| latent 节点 | `EmptySD3LatentImage`（16 通道，与 Flux latent 一致） |
| 采样 | KSampler，steps=8，cfg=1.0，euler / normal |
| 分辨率 | 768×1344（9:16 竖屏，对齐红果/抖音交付比例；成片 1080×1920 由 A100 侧超分） |

## 实测

| 指标 | 值 |
|---|---|
| 冷启动（含模型加载） | **31.73 s** |
| 热跑 | **22.56 s / 张** |
| 峰值显存 | **7713 / 8188 MiB**（DynamicVRAM 自动换入换出，默认策略即可，无需 `--lowvram`） |
| 出图 | `comfy/ComfyUI/output/zimage_smoke/smoke_0000{1,2}_.png` |

对照基线：Wan2.1-T2V-1.3B 热跑 55.4 s 出 2 秒 832×480 视频（`perf_wan_t2v_shot08.md`）。

## 目检结论

两张测试图（厨房调试机器人 / 车间仰视机甲）均达到**可直接进片的关键帧水准**：
- 电影感打光与真实浅景深，无 2024 年那种"塑料感/糊面"
- 提示词遵循准确：景别（中景/仰角全景）、光线方向（暖黄吊灯/晨光斜射）、材质细节（划痕油污/漂浮粉尘）都对上了
- 人手解剖正确（两张图都有清晰双手且无多指）
- 竖屏构图成立，主体位置适合短剧信息流

**结论：Z-Image-Turbo 作为 4070 代理关键帧模型的定位成立。**22.56 s/张 意味着 82 镜头 × 3 候选 ≈ 92 分钟跑完一集的代理关键帧，支撑"代理迭代—门禁—重抽"闭环。

## 坑与注意

- 运行 ComfyUI 前必须停掉 TTS(:8390) 与 L1 eval(:8391)，否则 8GB 卡放不下（串行错峰纪律）
- 峰值 7713 MiB 已贴近 8188 MiB 上限；若要提分辨率到 1024×1792，需改用 `z_image_turbo_int8_convrot.safetensors`（6.20 GB，已下载备用）
