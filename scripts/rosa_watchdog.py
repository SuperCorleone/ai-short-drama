"""rosa 看门狗：节点恢复即自动续跑，形成「反馈 → 推进待办」的闭环。

为什么需要它：icsnode06 一天内两次宕机（08-07 19:40、08-08）。在此之前
"监控"全靠我想起来 poll 一次——跨轮次不持续，节点恢复后可能白白空转几小时，
而这是借来的卡、宿主作业墙钟在走。

它做什么（每 POLL_SEC 一次，登录节点白名单命令，不打扰任何人）：
  1. `sinfo` 查 icsnode06 状态 + `squeue` 查持卡作业还在不在
  2. 状态变化写进 rosa_watchdog.log（带时间戳，便于事后复盘）
  3. **节点恢复的那一刻**自动按 ACTIONS 顺序续跑：
       - 重启 ComfyUI 长驻（后台）
       - 续跑 H3 权重下载（hf 断点续传，已下的不重来）
  4. 持卡作业消失（48h 墙钟到期/被砍）→ 记 CRITICAL 并停止动作，
     因为注入目标没了，此时任何续跑都会失败，继续重试只是刷屏

刻意不做的事：
  - 不提交任何新 Slurm 作业（纯 FIFO 队列下会跟 gaor 自己的其他项目抢卡）
  - 不碰宿主作业状态，不 kill 别人的进程
  - 登录节点上只跑 sinfo/squeue，间隔 >= 3 分钟

用法:
    comfy/venv/Scripts/python.exe scripts/rosa_watchdog.py --job 78208
    comfy/venv/Scripts/python.exe scripts/rosa_watchdog.py --job 78208 --once   # 单次探测
"""
import argparse
import datetime
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "pipeline", "rosa_watchdog.log")

SSH = ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none",
       "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", "rosa"]

HEALTHY = {"idle", "mixed", "allocated", "alloc", "comp"}

# 节点恢复后按顺序执行，每条**前台阻塞**跑完再下一条。
#
# ⚠️ 为什么不能用 `nohup ... &`：Slurm 按 **cgroup** 清理 job step，
# 脱离会话（setsid/nohup）逃不出 cgroup —— srun 一退进程立即被杀。
# 本项目 2026-08-08 已被这条坑过一次（下载在 5GB 处静默死亡）。
# 所以长任务必须跑在前台长驻的 srun 里，并依赖工具自身的断点续传。
#
# 每条都必须**幂等**：看门狗可能在一次宕机里触发多次，重复执行不能造成损坏。
ACTIONS = [
    ("续跑 H3 权重下载（前台阻塞，hf 断点续传）", (
        "export PYTHONNOUSERSITE=1 HF_HOME=/scratch/gaor/ai_short_drama/hf_cache "
        "PIP_CACHE_DIR=/scratch/gaor/ai_short_drama/pip_cache; "
        "cd /home/gaor/ai_short_drama; "
        "/scratch/gaor/ai_short_drama/venv/bin/python a100/fetch_models.py "
        "--tiers h3 --dest /scratch/gaor/ai_short_drama/models --max-workers 4"
    ), 7200),
]

# ComfyUI 长驻**不由看门狗负责**：它必须是一个前台长驻的 srun，
# 谁执行它谁就得一直挂着。看门狗要保持轻量与可中断，故拆成独立的 holder 任务
# （见 --hold 模式），二者分工不重叠。


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ssh_run(cmd, timeout=60):
    """在登录节点跑一条命令。返回 (rc, stdout)。

    注意：**绝不把结果接管道**——`ssh ... | grep` 的退出码取自管道末尾，
    会把 ssh 断连掩盖成 exit 0（本项目 2026-08-08 被这个坑到两次）。
    """
    try:
        p = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "<ssh timeout>"
    except Exception as e:
        return -2, f"<{type(e).__name__}: {e}>"


def probe(node, job):
    """返回 (node_state, flagged, job_state)。None 表示探测本身失败（网络/VPN）。

    ⚠️ 2026-08-08 修：node 参数只是**兜底**，真正要盯的是**持卡作业当前所在的节点**。
    初版把节点名写死成 icsnode06，结果作业被 requeue 后 Slurm 把它放到了 icsnode05，
    看门狗还在盯 icsnode06（仍 down*），于是记下自相矛盾的
    「节点 down* / 持卡作业 RUNNING」并且**永不触发续跑** ——
    盯着一台与当前工作无关的机器。集群里有两张 A100，作业换机是常态。
    """
    rc_n, out_n = ssh_run(f'squeue -j {job} -h -o "%N"')
    actual = out_n.strip() if rc_n == 0 and out_n.strip() else None
    node = actual or node

    rc, out = ssh_run(f'sinfo -N -n {node} -h -o "%T"')
    state = out.split()[0].strip("*~#$") .lower() if (rc == 0 and out) else None
    node_flagged = bool(rc == 0 and out and any(c in out for c in "*~"))

    rc2, out2 = ssh_run(f'squeue -j {job} -h -o "%T"')
    if rc2 != 0:
        job_state = None                      # 探测失败，别下结论
    elif not out2.strip():
        job_state = "GONE"                    # 队列里彻底没有了
    else:
        job_state = out2.strip().upper()[:7]  # RUNNING / PENDING / ...
    return state, node_flagged, job_state, node


def already_running(job, pattern):
    """远端是否已有同名任务在跑。避免看门狗与人工操作重复起同一个长任务。

    2026-08-08 实测：手动起了一次 H3 下载后，看门狗又因「目标未达成」起了第二次，
    远端出现 5 个 fetch 进程争同一批 .incomplete 文件（hf 有锁不至于损坏，
    但纯属浪费且难排查）。触发前必须先查。
    """
    rc, out = ssh_run(f'srun --jobid={job} --overlap bash -lc {shell_quote(f"pgrep -fc {pattern} || echo 0")}',
                      timeout=120)
    if rc != 0:
        return True   # 查不到就当作在跑，宁可不触发也不要重复起
    try:
        return int(out.strip().splitlines()[-1]) > 0
    except Exception:
        return True


def inject(job, title, script, timeout=180):
    log(f"  → {title}")
    rc, out = ssh_run(f"srun --jobid={job} --overlap bash -lc {shell_quote(script)}", timeout)
    tail = out.replace("\n", " | ")[-300:]
    log(f"    rc={rc} {tail}")
    return rc == 0


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


# 目标：h3 档的 5 个权重文件是否都已落盘（大小也要对得上，防半截文件）
GOAL_FILES = [
    ("diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors", 20_900_000_000),
    ("diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", 20_900_000_000),
    ("text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors", 27_000_000_000),
    ("vae/minimax_h3_video_vae_fp16.safetensors", 5_100_000_000),
    ("vae/minimax_h3_audio_vae_fp32.safetensors", 600_000_000),
]
MODELS_ROOT = "/scratch/gaor/ai_short_drama/models"

_goal_cache = {"met": False}


def goal_met():
    """h3 权重是否齐备。达成后缓存，避免每轮都去敲登录节点。"""
    if _goal_cache["met"]:
        return True
    checks = " ; ".join(
        f'test -f {MODELS_ROOT}/{f} && stat -c%s {MODELS_ROOT}/{f} || echo 0'
        for f, _ in GOAL_FILES)
    rc, out = ssh_run(f'bash -lc {shell_quote(checks)}')
    if rc != 0:
        return True  # 探测失败时不误触发续跑
    sizes = [int(x) for x in out.split() if x.isdigit()]
    if len(sizes) != len(GOAL_FILES):
        return False
    ok = all(s >= mn for s, (_, mn) in zip(sizes, GOAL_FILES))
    if ok:
        _goal_cache["met"] = True
        log("目标达成：h3 权重 5/5 齐备")
    return ok


def hold_comfy(job):
    """ComfyUI 长驻：前台 srun，断了就重连重启。

    单独成一个模式而不塞进看门狗，是因为它必须一直占着一条 ssh 连接；
    看门狗要保持轻量、可随时中断，两者职责不能混。

    注：holder 靠 `--jobid` 注入，srun 会自动跟到作业当前所在节点，
    所以作业换机（icsnode06 → icsnode05）时它能自愈，无需改动 —— 实测已验证。
    """
    script = ("S=/scratch/gaor/ai_short_drama; "
              "export PYTHONNOUSERSITE=1 HF_HOME=$S/hf_cache "
              "PIP_CACHE_DIR=$S/pip_cache; cd $S/ComfyUI; "
              "exec $S/venv/bin/python main.py --port 8189 --listen 127.0.0.1")
    log(f"ComfyUI holder 启动（job={job}），断线自动重连")
    fails = 0
    waiting = False
    while True:
        # 节点宕机 / 作业被 requeue 期间的失败**不算 holder 的错**，不计入熔断计数。
        # 初版把它们一起计数，节点宕机 10 分钟就把 holder 熔断掉了 ——
        # 而 icsnode06 两天宕了三次，那等于 holder 基本不可能活着等到节点回来。
        rc0, out0 = ssh_run(f'squeue -j {job} -h -o "%T"')
        jstate = out0.strip().upper() if rc0 == 0 else ""
        if rc0 != 0 or not jstate:
            if not waiting:
                log(f"持卡作业 {job} 不可见（探测失败或已出队），等待…")
                waiting = True
            time.sleep(120)
            continue
        if not jstate.startswith("RUN"):
            if not waiting:
                log(f"持卡作业 {job} 处于 {jstate}，等它回到 RUNNING 再挂 ComfyUI")
                waiting = True
            time.sleep(120)
            continue
        waiting = False

        rc, out = ssh_run(f"srun --jobid={job} --overlap bash -lc {shell_quote(script)}",
                          timeout=None)
        fails = fails + 1 if rc != 0 else 0
        log(f"ComfyUI holder 退出 rc={rc}（作业 RUNNING 状态下连续失败 {fails}）{out[-200:]}")
        if fails >= 10:
            log("CRITICAL 作业在跑但 holder 连续失败 10 次，停止重试（多半是 ComfyUI 本身起不来）")
            return 2
        time.sleep(60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="持卡作业 JobID（注入目标）")
    ap.add_argument("--node", default="icsnode06")
    ap.add_argument("--poll", type=int, default=300, help="探测间隔秒（>=180，别敲登录节点）")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--hold", action="store_true",
                    help="ComfyUI 长驻模式：前台跑 ComfyUI，掉了自动重连重启（与看门狗分开跑）")
    args = ap.parse_args()

    if args.hold:
        return hold_comfy(args.job)
    poll = max(180, args.poll)

    log(f"watchdog 启动 node={args.node} job={args.job} poll={poll}s")
    last_state = None
    last_healthy = None
    last_job_state = None
    last_node = None

    while True:
        state, flagged, job_state, cur_node = probe(args.node, args.job)
        healthy = (state in HEALTHY) and not flagged

        # 2026-08-08 修：初版把「非 RUNNING」一律当成永久消失就退出，太脆。
        # 节点宕机会让作业被 **requeue 成 PENDING**，它会自己回来——
        # 实测 78208 一天内被 requeue 三次。只有队列里彻底没有了才是真的没了。
        if job_state == "GONE":
            log(f"CRITICAL 持卡作业 {args.job} 已不在队列 —— 注入目标永久消失，看门狗停止。"
                f"需人工确认新的持卡作业 JobID 后重启。")
            return 2
        if job_state and job_state != "RUNNING":
            if last_job_state != job_state:
                log(f"持卡作业 {args.job} 处于 {job_state}（多半是节点宕机后被 requeue），等它回来")
            last_job_state = job_state
            last_healthy = False   # 作业不在跑 = 不健康，回来时才会走恢复沿
            if args.once:
                return 1
            time.sleep(poll)
            continue
        last_job_state = job_state

        if cur_node != last_node and last_node is not None:
            log(f"⚑ 持卡作业换机：{last_node} → {cur_node}（requeue 后 Slurm 重新分配）")
        last_node = cur_node
        if state != last_state or healthy != last_healthy:
            flag = "*" if flagged else ""
            log(f"节点 {cur_node}: {state}{flag}  健康={healthy}  持卡作业={job_state}")

        # 触发条件有两个，缺一不可：
        #   ① 恢复沿（不健康 → 健康）—— 宕机后自动接上
        #   ② **目标未达成**（预期产物还缺）—— 节点一直健康、但活儿被 ssh 断连打断时接上
        # 只有 ① 会漏掉第二种情况：2026-08-08 实测 H3 下载因 ssh 断连停在 1/5，
        # 而节点全程健康，看门狗因此不会触发 —— 事件驱动不够，必须目标驱动。
        recovered = healthy and last_healthy is False
        unfinished = healthy and not goal_met()
        if recovered or unfinished:
            log(f"触发续跑（恢复沿={recovered} 目标未达成={unfinished}）：")
            for title, script, tmo in ACTIONS:
                if already_running(args.job, "fetch_models.py"):
                    log("    [skip] 远端已有下载在跑，本轮不重复触发")
                    break
                if not inject(args.job, title, script, tmo):
                    log(f"    [warn] {title} 未成功，下轮恢复沿会重试")
                time.sleep(5)

        last_state, last_healthy = state, healthy
        if args.once:
            return 0 if healthy else 1
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
