#!/bin/bash
# A100 作业 —— **瘦壳 sbatch**（2026-08-10 改为排队模式）
#
# 整个脚本只有一行有效指令，故意的：Slurm 在提交时把 sbatch 脚本存入假脱机，
# 事后编辑不影响已排队/运行的作业（R6「sbatch 提交即固化」）。
# 真正的内容在 payload.sh 里 —— **排队期间可以继续改渲染逻辑而不必 scancel 重排**。
# 在只有 2 张 A100 的 FIFO 集群上，重排一次可能损失一到两天。
#
# ── 2026-08-10 模式变更：注入 → 排队 ──────────────────────────────
# R4 原纪律是「不排队、只做任务注入」，前提是 gaor 名下有持卡作业可注入。
# 实测 2026-08-10：78208/78222/78224 全部结束，**gaor 名下零作业** → 注入目标消失，
# 排队成为唯一途径。同时 gpu 分区 PD 队列为空 → 此刻提交即队首。
#
# ── 为什么硬绑 icsnode06（2026-08-10 用户指令）────────────────────
# 代价已知并接受：icsnode06 被 marzit 的 78345 占走全部 120G 内存（1 CPU、0 GPU），
# 要等它跑完（最多约 46.7h）；而 icsnode05 的 78344 约 32.5h 后就结束。
# 风险：硬绑后若 icsnode06 再次宕机（两天三宕前科），作业会死等而不会 fallback 到 05。
#
# 提交:  sbatch a100/job.sbatch
# 续跑:  同一条命令再提一次即可，各 stage 读 manifest 跳过已完成项
#
#SBATCH --job-name=asd-h3
#SBATCH --output=/home/gaor/ai_short_drama/logs/asd_%j.out
#SBATCH --error=/home/gaor/ai_short_drama/logs/asd_%j.err
#SBATCH --partition=gpu
#SBATCH --nodelist=icsnode06
#SBATCH --gres=gpu:NVIDIAA100-PCIE-40GB:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=2-00:00:00
#SBATCH --requeue

bash /home/gaor/ai_short_drama/a100/payload.sh
