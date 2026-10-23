"""排队作业观察器（2026-08-10 新增，配合 78351 `asd-h3` 的**排队模式**）。

## 为什么不用 rosa_watchdog.py

`rosa_watchdog.py` 是为**注入模式**写的：它假定 `--job` 是一个正在 RUNNING 的持卡
作业，一旦发现"节点健康且目标未达成"就注入 `fetch_models.py --max-workers 4`。

在排队模式下这是**有害**的：78351 的 `payload.sh` 第 1 步自己就在补 H3 权重。
两边同时跑 = 远端出现两个 fetch 进程 —— 正是 2026-08-08 搞挂 icsnode05 的
「单点约束守住、聚合约束失守」原样重演（R4.5 #1）。`already_running()` 只是缓解，
不是消除：它有竞态窗口，且探测失败时才 fail-safe。

**所以这个脚本的第一条设计约束是：只观察，不触发任何远程动作。**
它不注入、不提交、不下载、不重启任何东西。作业自己是自驱的（payload.sh 全流程
自动化 + 断点续跑），监控的唯一职责是"告诉我发生了什么"。

## 第二条：探测频率随距离自适应

作业预计 08-12 10:41 起跑，从提交算起约 40 小时。定频 5 分钟探测 = 480 次敲登录节点，
纯浪费——R4.5 #2 明确把"多个后台任务各自 ssh 到同一台登录节点"列为危险行为，
而 R4.5 #3 把"故障期定频重试"列为故障放大。距离还远时就该退得很稀：

    PENDING 且距 ETA > 2h   → 30 分钟一次
    PENDING 且距 ETA <= 2h  → 10 分钟一次
    RUNNING                 → 5 分钟一次（此时才值得密切看）
    探测失败（VPN/登录节点） → 指数退避，封顶 30 分钟

40 小时下来总敲击约 90 次，平均 27 分钟一次。

## 第三条：终态即退出，不空转

作业进入 COMPLETED/FAILED/CANCELLED/TIMEOUT 就记录并退出。
但 **PENDING → 消失** 与 **RUNNING → 消失** 含义完全不同，必须用 `sacct` 查真实终态，
不能看到"squeue 里没了"就下结论——节点宕机导致的 requeue 会让作业短暂不可见。

用法:
    python scripts/watch_queued_job.py --job 78351
    python scripts/watch_queued_job.py --job 78351 --once
"""
import argparse
import datetime
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from rosa_guard import light, backoff_sleep  # noqa: E402

LOG = os.path.join(ROOT, "pipeline", "job_watch.log")

POLL_FAR = 1800      # PENDING 且距 ETA > 2h
POLL_NEAR = 600      # PENDING 且距 ETA <= 2h
POLL_RUN = 300       # RUNNING
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
            "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED"}


def log(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def poll(job):
    """一次探测。返回 dict；rc!=0 的字段为 None 表示这次没问出来，**不等于事实为假**。"""
    # 一条 squeue 拿全所有字段，别拆成多条——每多一条就是多敲一次登录节点
    rc, out = light(f'squeue -j {job} -h -o "%T|%N|%S|%R|%M|%L"')
    if rc != 0:
        return {"probe_ok": False}
    if not out.strip():
        return {"probe_ok": True, "state": "GONE"}
    f = (out.strip().splitlines()[0] + "|||||").split("|")
    return {"probe_ok": True, "state": f[0].upper(), "node": f[1].strip(),
            "start": f[2].strip(), "reason": f[3].strip(),
            "elapsed": f[4].strip(), "left": f[5].strip()}


def final_state(job):
    """作业从 squeue 消失后查真实终态。squeue 只保留在途作业，终态要问 sacct。"""
    rc, out = light(f'sacct -j {job} -X -n -o State,ExitCode,Elapsed,NodeList')
    if rc != 0 or not out.strip():
        return None
    return out.strip().splitlines()[0].strip()


def eta_seconds(start_str):
    """距 Slurm 预估起跑时间还有多少秒。解析不了就当作很远。"""
    try:
        t = datetime.datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None
    # Slurm 返回的是集群本地时间；本机与集群时区可能不同，这里只用于**粗略**决定
    # 探测频率，偏几小时不影响正确性（最坏结果是多探或少探几次）
    return (t - datetime.datetime.now()).total_seconds()


def tail_status(job, lines=6):
    """作业跑起来后读它的 status 文件。**单文件 tail 是 R5 白名单操作**（KB 级）。"""
    path = f"/home/gaor/ai_short_drama/logs/status_{job}.txt"
    rc, out = light(f"tail -{lines} {path}")
    return out if rc == 0 else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    log(f"=== 观察器启动 job={args.job}（只观察，不触发任何远程动作）===")
    last = {}
    fails = 0
    running_seen = False

    while True:
        st = poll(args.job)

        if not st["probe_ok"]:
            fails += 1
            log(f"探测失败（第 {fails} 次）—— VPN 或登录节点问题，指数退避")
            if args.once:
                return 1
            backoff_sleep(fails)     # 故障期绝不定频重试（R4.5 #3）
            continue
        fails = 0

        state = st["state"]

        # ---- 从队列消失：必须用 sacct 确认真实终态 ----
        # PENDING 消失可能是被砍，RUNNING 消失可能是正常结束，也可能是节点宕机后
        # 尚未 requeue 回来的空窗。看到"没了"就下结论是 2026-08-08 那类误判的温床。
        if state == "GONE":
            fin = final_state(args.job)
            if fin is None:
                log("作业不在 squeue 且 sacct 查不到 —— 可能是 requeue 空窗，下轮再看")
                if args.once:
                    return 1
                time.sleep(POLL_NEAR)
                continue
            log(f"=== 作业终止 === sacct: {fin}")
            if running_seen:
                log("最后的 status 输出：\n" + tail_status(args.job, 25))
            return 0 if fin.split()[0] in ("COMPLETED",) else 2

        # ---- 状态变化才记录，避免刷屏 ----
        changed = [k for k in ("state", "node", "reason", "start")
                   if last.get(k) != st.get(k)]
        if changed:
            log(f"{state} node={st.get('node') or '-'} reason={st.get('reason') or '-'} "
                f"预计起跑={st.get('start') or '-'} 已跑={st.get('elapsed')} 余={st.get('left')}")

        if state == "RUNNING":
            if not running_seen:
                log(f"🎉 作业已起跑于 {st.get('node')} —— payload.sh 开始自动执行"
                    f"（补 VAE → 同步参考图 → 起 ComfyUI → S2 → 长驻）")
                running_seen = True
            body = tail_status(args.job)
            if body and body != last.get("_status"):
                log("status:\n" + body)
                last["_status"] = body
            wait = POLL_RUN
        elif running_seen and state == "PENDING":
            # RUNNING → PENDING = 节点宕机被 requeue。icsnode06 有两天三宕前科，
            # 这不是异常路径而是预期路径，payload.sh 的断点续跑就是为它准备的。
            log(f"⚠ 作业从 RUNNING 退回 PENDING（reason={st.get('reason')}）"
                f" —— 多半是 icsnode06 又宕了，等 Slurm 重新调度")
            running_seen = False
            wait = POLL_NEAR
        else:
            eta = eta_seconds(st.get("start", ""))
            wait = POLL_NEAR if (eta is not None and eta <= 7200) else POLL_FAR

        last.update({k: st.get(k) for k in ("state", "node", "reason", "start")})
        if args.once:
            return 0
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
