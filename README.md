# AI 短剧生成管线：评估门禁 + Godot 预演环境（实验工程）

把网文改编成竖屏 AI 短剧的 9 层管线（L0 叙事 → L8 评估门禁）。本仓库只收**自研代码、规格文档与实测报告**；模型权重、虚拟环境、引擎二进制与生成媒体不入库，还原方式见下文。

> **现状**：评估门禁（L8 的 L0 + L1 两级）、Godot 预演环境（L2）、代理关键帧（L3）与 A100 批渲染链路已实测跑通；**尚未训练任何模型**——按“评估先于训练”的顺序，先证明打分信号可信，再训练策略。
> 全部实测结论与证据见 [`TASKS.md`](TASKS.md) 第三部分「结果与证据」，该文档是本项目唯一权威文档。

## 目录

| 路径 | 内容 |
|---|---|
| [`TASKS.md`](TASKS.md) | 规格（§1–§8）、执行台账、结果与证据、集群纪律 |
| [`ai_short_drama.agent.final.md`](ai_short_drama.agent.final.md) | 前期调研与方案校准报告（2026-07-22） |
| [`eval/`](eval/) | L8 评估门禁：`gate.py` 三态门禁（PASS / RETRY / REJECT，L0 规格硬校验 + L1 五指标阈值，自检 21/21）；`l1_server.py` 五指标服务（多语言 CLIP / DINOv2 / ArcFace / RAFT / MUSIQ）；`verify_bible.py` 分档身份门禁；`eval_h3_s2.py` |
| [`third_party/godot-mcp/`](third_party/godot-mcp/) | 基于 Coding-Solo/godot-mcp 修改的 MCP 服务：26 个工具，其中 12 个自研（灯光、相机、注视约束、动画、渲染、录制、辅助缓冲、批量建场、批量渲染） |
| [`godot/projects/`](godot/projects/) | Godot 4.7.1 工程：MCP 冒烟工程 + 首集 5 个灰盒场景 |
| [`pipeline/previs/`](pipeline/previs/) | L2 预演：`build_previs.mjs`（MCP stdio 客户端，景别 / 视角 → 相机映射）、`shots.json`（82 镜头规格卡）、`make_canny.py`、渲染与通道报告 |
| [`pipeline/keyframe/`](pipeline/keyframe/) | L3 代理关键帧：`l3_prompt.py` 提示词构建、`run_zimage.py`、`eval_keyframes.py`、L1 实测报告 |
| [`pipeline/assets/`](pipeline/assets/) | L1 角色圣经：基准脸穷举选择、跨角色相似度报告 |
| [`pipeline/animatic/`](pipeline/animatic/) | animatic 合成、语速标定、节奏报告 |
| [`pipeline/shots/`](pipeline/shots/) | A100 上 MiniMax-H3 Ref2VA 的 L1 实测记录（参考图接线修复前后对照、首集续跑快照） |
| [`pipeline/deliver/`](pipeline/deliver/) | AI 标识三处注入与自检 |
| [`pipeline/draft/`](pipeline/draft/) | 4070 上 Wan2.1-1.3B 草稿档性能记录 |
| [`a100/`](a100/) | A100（SLURM）批作业：瘦壳 sbatch、镜头级断点续跑、H3 驱动、权重清单 `models.json`、ComfyUI API 工作流 |
| [`scripts/`](scripts/) | 分镜解析与 action 回填、`rosa_guard.py`（集群操作唯一入口：并发闸、健康拒绝、指数退避、审计）、GPU 互斥、MCP 冒烟测试 |
| [`tts/`](tts/) | F5-TTS HTTP 服务 |
| [`素材/小说/精神小妹也要修机甲吗/产出/`](素材/小说/精神小妹也要修机甲吗/产出/) | L0 改编产物：资产提取、大纲、剧本、分镜脚本（`scripts/parse_shotlist.py` 的输入） |
| [`docs/media/`](docs/media/) | 本页用到的预览图 |

## 关键结果

| 结论 | 证据 |
|---|---|
| 门禁能分辨好坏：A100 好图 PASS，SageAttention 全黑图 RETRY，阈值边界自检 21/21 | [`eval/gate.py`](eval/gate.py) |
| 英文 CLIP 对中文提示零鉴别力（切题 0.1933 < 完全不相关 0.2017），换多语言 CLIP | TASKS.md 第三部分 3.2 |
| SageAttention 在 A100 sm_80 上只提速 6.1%，MUSIQ 64.51 → 22.32、出图全黑，已回滚 | TASKS.md 第三部分 3.2 |
| 82 张代理关键帧 MUSIQ 中位 73.00，82/82 高于阈值 55；clip_t 最低的两张追溯为数据缺陷 | [`pipeline/keyframe/l1_report.md`](pipeline/keyframe/l1_report.md) |
| `render_shot_batch`：246 次 Godot 进程启动 → 5 次，9 分钟 → 41.5 秒 | TASKS.md 第三部分 3.1 |
| SLURM 同节点不同 step 共享网络命名空间，长驻 ComfyUI 后首张 276 s → 之后 56 s/张 | TASKS.md 第三部分 3.1 |

### Godot 预演四通道（G01-S01）

| rgb | depth | normal | canny |
|---|---|---|---|
| <img src="docs/media/previs_G01-S01_rgb.png" width="180"> | <img src="docs/media/previs_G01-S01_depth.png" width="180"> | <img src="docs/media/previs_G01-S01_normal.png" width="180"> | <img src="docs/media/previs_G01-S01_canny.png" width="180"> |

### 参考图接线静默失效（MiniMax-H3 Ref2VA，G01-S05 特写，同 seed 同提示词）

| 基准脸 | 修复前 | 修复后 |
|---|---|---|
| <img src="docs/media/h3_G01-S05_ref_face.jpg" width="200"> | <img src="docs/media/h3_G01-S05_broken_refs_t2.6.jpg" width="300"> | <img src="docs/media/h3_G01-S05_fixed_t2.6.jpg" width="300"> |
| — | ArcFace 0.1931 | ArcFace 0.6668 |

修复前 4/4 正常出片、无任何报错，但与 `--n-refs 0` 零参考对照臂的视频 / 音频流 MD5 逐位相同，即参考图影响严格为零。原因是 `ref_images` 用了嵌套 dict 写法，正确写法是点号键名 `ref_images.ref_image_<i>`（见 [`a100/run_h3_ref2va.py`](a100/run_h3_ref2va.py)）。这是单个特写镜头的数据点，中景 G04-S05 修复后为 0.0970 → 0.2773，数据见 [`pipeline/shots/h3_s2/l1_report_fixed.json`](pipeline/shots/h3_s2/l1_report_fixed.json)。

## 环境还原

以下内容不入库：

| 组件 | 版本 / 位置 | 还原方式 |
|---|---|---|
| ComfyUI | comfyanonymous/ComfyUI @ `2e47082`，放在 `comfy/ComfyUI`，venv 为 `comfy/venv`（Python 3.10.11，torch 2.11.0+cu128） | 克隆后 `pip install -r requirements.txt` |
| 模型权重 | 本机代理档与草稿档 / A100 | `scripts/download_models_p1.py`、`scripts/download_models.py` / `a100/fetch_models.py` + `a100/models.json` |
| Godot | 4.7.1-stable win64 | 从官网下载，放到 `godot/Godot_v4.7.1-stable_win64.exe`（脚本按此路径查找） |
| godot-mcp | `third_party/godot-mcp`，Node ≥ 18 | 在该目录执行 `npm ci`，会自动 `npm run build` 生成 `build/` |
| L1 评估栈 | `eval/venv` | `pip install -r eval/requirements.txt` |
| TTS | `tts/venv`（torch 2.8.0+cu128，f5-tts 1.1.22） | `pip install f5-tts==1.1.22`；默认参考音频 `tts/ref/basic_ref_en.wav` 取自 F5-TTS 仓库示例 |
| A100 作业 | SLURM | 见 [`a100/README.md`](a100/README.md) |

同样不入库的还有：生成媒体（关键帧、角色圣经图、预演通道图、视频片段、配音）、运行日志与审计日志、备份与中间快照、小说原文及其逐字整理稿。报告中引用的这类路径在本仓库中不存在。

## 第三方组件与许可

- `third_party/godot-mcp`：MIT（见其 [LICENSE](third_party/godot-mcp/LICENSE)），基于上游 commit `1209744`，修改集中在 `src/index.ts` 与 `src/scripts/godot_operations.gd`。
- 研发期使用的部分模型为非商用许可（F5-TTS cc-by-nc-4.0、InsightFace buffalo_l 非商业研究许可），产品化替换清单见 TASKS.md §2。
