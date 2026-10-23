# A100 侧作业（rosa · icsnode05/06 · A100-PCIE-40GB）

> 依据用户 2026-08-04 指令：**「把本地跑不下的都放到 A100 上跑」**。
> ⚠️ **rosa 当前仍冻结**（用户在跑更重要的任务）。本目录是"解冻即可提交"的成品，尚未在 rosa 上执行过任何命令。

## 为什么这些东西必须上 A100

本机 4070 Laptop 只有 **5.8 GB 可用显存**。下表是逐项核算后的结论，不是估计：

| 工作 | 模型 | fp8 显存需求 | 4070 | A100-40G |
|---|---|---|---|---|
| 角色圣经派生（三视图/表情/服装） | Qwen-Image-Edit-2511 20B | ~29.9 GB | ❌ | ✅ |
| L3 终版关键帧 | Qwen-Image-2512 20B | ~29.8 GB | ❌ | ✅ |
| L3 结构控制 | Qwen-Image ControlNet-Depth | +3 GB | ❌ | ✅ |
| L4 图生视频终渲 | Wan2.2-I2V-A14B（双专家串行） | ~21 GB | ❌ | ✅ |
| L4 快速粗筛 | LTX-2.3 22B | **46.15 GB bf16 装不下**，须 GGUF Q8 ~23 GB | ❌ | ⚠️ 仅 GGUF |
| L5 音频驱动口型 | Wan2.2-S2V-14B | ~23 GB | ❌ | ✅ |
| L6 视频超分 | SeedVR2-3B | ~7 GB | 勉强但没意义 | ✅ |
| L2 自动评估 | VideoScore2 | ~16 GB | ❌ | ✅ |
| 角色 LoRA 训练 | Qwen-Image / Wan2.2 | >24 GB | ❌ | ✅ |

**留在本机的**：Godot 预演（L2）、Z-Image-Turbo 代理关键帧（22.6 s/张，已验证）、F5-TTS、L1 五指标评估、剪辑装配（L7）。
这正是 PLAN_v2 架构定律 2 ——「迭代在代理上做，终渲在 A100 上做」。

## 目录内容

| 文件 | 作用 |
|---|---|
| `models.json` | 权重清单，分 tier1/2/3。体积为 2026-08-04 从 HF API **实测**取得 |
| `fetch_models.py` | 分层拉取器，幂等 + 断点续传 + split_files 扁平化 |
| `00_fetch_models.sbatch` | **CPU-only 拉取作业**（`--gres=none`） |
| `job.sbatch` | **瘦壳** 渲染作业，只有一行 `bash payload.sh` |
| `payload.sh` | 真正的作业内容，**排队期间可随意编辑** |
| `render_shots.py` | 镜头级断点续跑渲染器 |
| `workflows/` | 各阶段 ComfyUI 工作流模板（**须先在本机验证格式再上传**） |

## 三条设计要点

**1. 下载绝不占 A100。**
`00_fetch_models.sbatch` 用 `--gres=none`，与 GPU 队列完全不冲突。rosa 结构上只有 1–2 张 A100 且与 tratto 争抢，把几百 GB 下载放进 GPU 作业 = 拿最稀缺的资源做最不需要它的事。这个作业可以先跑、随便跑、跑挂了重跑。

**2. 瘦壳 sbatch 绕开"提交即固化"。**
Slurm 在提交时把 sbatch 脚本存入假脱机，事后编辑不影响已排队作业。`job.sbatch` 只有一行调 `payload.sh`，于是排队期间还能继续改渲染逻辑，不必 `scancel` 重排——在争抢中的 A100 上重排一次可能损失半天。

**3. 镜头级断点续跑。**
每个镜头产出立即落盘 `/scratch` 并**立即** fsync 一行到 `manifest.jsonl`。作业被 time limit 砍掉或节点故障后，重新 `sbatch` 即跳过 `status=ok` 的镜头。另外 `render_shots.py` 捕获 SIGTERM（Slurm 时限将至先发 TERM），当前镜头跑完即停，保证 manifest 一致。连续失败 5 次熔断——连续失败通常意味着环境坏了，继续跑只是空烧机时。

## 解冻后的执行顺序

```bash
# 0. 建工作区（若尚未建）
ssh -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=15 -o BatchMode=yes rosa \
    'mkdir -p /scratch/gaor/ai_short_drama/{models,hf_cache,outputs} /home/gaor/ai_short_drama/logs'

# 1. 代码上传（git 中转或 scp；只放 /home/gaor/ai_short_drama 内，绝不碰其他目录）

# 2. CPU-only 拉权重（不排 GPU 队，可立即跑）
sbatch a100/00_fetch_models.sbatch tier1

# 3. 在计算节点装 ComfyUI 到 /scratch/gaor/ai_short_drama/ComfyUI，
#    并把 models 软链到 /scratch/gaor/ai_short_drama/models

# 4. 排 GPU 队跑渲染
STAGE=bible sbatch a100/job.sbatch      # 先补角色圣经派生（P1 遗留）
STAGE=keyframe sbatch a100/job.sbatch   # 再 L3 终版关键帧
STAGE=i2v sbatch a100/job.sbatch        # 再 L4 图生视频

# 5. 查队列（登录节点只做轻量操作）
squeue -u gaor
```

## rosa 纪律（复述，防止误操作）

- 本项目一切文件只放 `/home/gaor/ai_short_drama`（代码）与 `/scratch/gaor/ai_short_drama`（权重/输出）
- **不得读取、修改、删除工作区之外的任何文件** —— `/home/gaor` 下的 tratto/rl 是另一项在研工作
- **不得 `scancel` 或干预他人作业**（tratto-cardD/cardE 等），本项目作业一律排队等待
- 登录节点仅限 `squeue/sinfo/sacct/scontrol/tail/sbatch/scp`；绝不在其上训练/加载模型/大下载
- SSH 必须禁用多路复用：`ssh -o ControlMaster=no -o ControlPath=none -o ConnectTimeout=15 -o BatchMode=yes rosa`
- 4090 与 rosa 的 VPN 互斥问题已随 4090 退出而消失

## 待办

- [ ] `workflows/bible.json` — Qwen-Image-Edit-2511 多图参考派生（**格式须先在本机用小模型验证**）
- [ ] `workflows/keyframe.json` — Qwen-Image-2512 + ControlNet-Depth（吃 Godot 深度图）
- [ ] `workflows/i2v.json` — Wan2.2-I2V-A14B 首尾帧
- [ ] 在计算节点安装 ComfyUI 的一次性脚本
