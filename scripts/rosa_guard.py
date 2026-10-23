"""rosa 操作唯一入口：把「聚合约束」变成代码强制，而不是靠我记得。

2026-08-08 事故：远端同时出现 5 个 fetch 进程 × 每个 max_workers=4 ≈ 20 条并行流
写 /scratch，icsnode05（此前连续健康 1 天 21 小时）随即 mixed* → down*。
我**按单进程遵守了 max_workers=4，却让进程数失控**——单点约束守住了，聚合约束没有。

这个脚本存在的意义：**任何对 rosa 的远程操作都必须经由它**，由它强制以下事项。
靠纪律记住是不够的——事故当天我刚写完"绝不单流拉多 GB"的纪律，转身就把并发放大了 5 倍。

强制项:
  1. **全局并发闸**：同时进行的远程重操作 <= MAX_HEAVY（默认 1）。基于文件锁，
     跨进程有效，我起多少个后台任务都逃不掉
  2. **健康前置检查**：节点非健康时**拒绝**发起重操作（不是重试，是拒绝）
  3. **故障期指数退避**：连续失败时退避时间翻倍，封顶 BACKOFF_MAX。
     故障期定频重试等于在机器生病时加压
  4. **审计日志**：每次远程操作记录时间/类型/时长/结果，事后可复盘归因
  5. **登录节点白名单**：轻操作（sinfo/squeue/ls/stat）不占并发闸，但也记账

刻意不做:
  - 不提交新 Slurm 作业
  - 不碰宿主作业与他项目文件

用法（库）:
    from rosa_guard import light, heavy, node_healthy
    rc, out = light('squeue -u gaor -h -o "%T"')
    rc, out = heavy(78208, 'python3 a100/fetch_models.py --tiers h3', timeout=7200)
"""
import datetime
import json
import os
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.path.join(ROOT, "pipeline", "rosa_audit.jsonl")
LOCK = os.path.join(ROOT, "pipeline", ".rosa_heavy.lock")

SSH = ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none",
       "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", "rosa"]

MAX_HEAVY = 1          # 同时最多几个远程重操作。**不要调大**，事故就是从 5 个开始的
BACKOFF_BASE = 60
BACKOFF_MAX = 1800
HEALTHY = {"idle", "mixed", "allocated", "alloc", "comp"}


def _audit(kind, cmd, rc, secs, extra=None):
    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "kind": kind, "rc": rc, "seconds": round(secs, 1), "cmd": cmd[:300]}
    if extra:
        rec.update(extra)
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _run(cmd, timeout):
    """执行一条远程命令。

    **绝不接管道**——`ssh ... | grep` 的退出码取自管道末尾，会把 ssh 断连
    掩盖成 exit 0（2026-08-08 被坑两次，两次都误判成"任务完成"）。
    """
    t0 = time.time()
    try:
        # encoding 必须显式写 utf-8：text=True 在 Windows 上按本地 GBK 解码，
        # 远端的中文日志会整段变成乱码。**读错远端状态正是这个护栏该防的事**
        # （R4.5 #8 的同类：以为读到了，其实读到的是垃圾）。
        p = subprocess.run(SSH + [cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            # 失败时必须带上 stderr：R5 要求「诊断别猜」——
            # `Connection closed` = 登录节点被压垮、`timed out` = 网络/VPN、
            # `Permission denied` = 认证，三者处置完全不同。只看 rc=255 分不出来。
            err = (p.stderr or "").strip()
            out = (out + "\n" + err).strip() if err else out
        return p.returncode, out, time.time() - t0
    except subprocess.TimeoutExpired:
        return -1, "<ssh timeout>", time.time() - t0
    except Exception as e:
        return -2, f"<{type(e).__name__}: {e}>", time.time() - t0


def light(cmd, timeout=60):
    """登录节点白名单轻操作（sinfo/squeue/ls/stat/tail）。不占并发闸，但记账。

    注意：**并发也是负载**。单条轻量不代表整体轻量——多个后台任务各自
    ssh 到同一台登录节点，聚合起来照样能压垮它（handoff 明确点过这条）。
    所以轻操作也走这里，审计日志能看出我是不是敲得太频。
    """
    rc, out, secs = _run(cmd, timeout)
    _audit("light", cmd, rc, secs)
    return rc, out


MAX_PUT_BYTES = 1_048_576   # 1 MB。见 put() 的说明——这个上限是护栏，不是参数


def put(local_path, remote_path, timeout=120):
    """把**小文件**推到 rosa（脚本 / 配置 / 分镜表）。

    R5 白名单只允许"几 KB 的脚本写入与 scp"，大文件传输是压垮登录节点的主因
    （管理员确认）。所以这里硬性卡 1 MB：**权重、产出、日志一律不准走这条路**，
    它们各自就地下载或留在 /scratch。

    上限做成代码而不是纪律，是 2026-08-08 的教训——那天我刚写完"绝不单流拉多 GB"，
    转身就把并发放大了 5 倍。**能被我"就这一次"绕过的约束等于不存在。**
    """
    size = os.path.getsize(local_path)
    if size > MAX_PUT_BYTES:
        _audit("put-refused", f"{local_path} -> {remote_path}", -6, 0.0, {"bytes": size})
        return -6, (f"拒绝传输：{os.path.basename(local_path)} 有 {size/1e6:.1f} MB，"
                    f"超过 {MAX_PUT_BYTES/1e6:.0f} MB 上限。登录节点只走小文件（R5）。")

    scp = ["scp", "-o", "ControlMaster=no", "-o", "ControlPath=none",
           "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
           local_path, f"rosa:{remote_path}"]
    t0 = time.time()
    try:
        p = subprocess.run(scp, capture_output=True, text=True, timeout=timeout)
        rc, out = p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        rc, out = -1, "<scp timeout>"
    _audit("put", f"{local_path} -> {remote_path}", rc, time.time() - t0, {"bytes": size})
    return rc, out


MAX_GET_BYTES = 33_554_432   # 32 MB，单次调用的**总量**上限

def get(remote_paths, local_dir, timeout=300):
    """把**镜头级产物**取回本机做 L1 评估（ArcFace / RAFT / MUSIQ 都跑在 4070 上）。

    为什么需要这个口子：评估栈在本机，而产物生成在 A100。不取回来就只能靠
    退出码判断成败——那正是 R4.5 #8 明令禁止的「以返回码代替产物核验」。
    目检更是非取回不可，人得真看见画面。

    为什么仍要设上限：R5 的本意是**登录节点不能当批量数据通道**。单集 82 镜头
    的终渲成片是 GB 级，那种量必须留在 /scratch 或走别的通道，不准走这里。
    32 MB 够取几个镜头的样片做门禁判定，不够搬运整集——这个边界是刻意的。

    权重、HF 缓存、整集产出一律不准走这条路（与 put() 同一条纪律的下行版本）。
    """
    if isinstance(remote_paths, str):
        remote_paths = [remote_paths]
    os.makedirs(local_dir, exist_ok=True)
    scp = ["scp", "-o", "ControlMaster=no", "-o", "ControlPath=none",
           "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]
    scp += [f"rosa:{p}" for p in remote_paths] + [local_dir]
    t0 = time.time()
    try:
        p = subprocess.run(scp, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        rc, out = p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        rc, out = -1, "<scp timeout>"

    # **核验产物而不是信返回码**：scp 成功了也要看文件真的落地、且总量没越界
    got = []
    for rp in remote_paths:
        lp = os.path.join(local_dir, os.path.basename(rp))
        if os.path.exists(lp):
            got.append((lp, os.path.getsize(lp)))
    total = sum(s for _, s in got)
    if total > MAX_GET_BYTES:
        _audit("get-oversize", str(remote_paths)[:300], -6, time.time() - t0, {"bytes": total})
        return -6, (f"取回 {total/1e6:.1f} MB，超过 {MAX_GET_BYTES/1e6:.0f} MB 上限。"
                    f"文件已落地但**这次操作不该发生**——批量产出请留在 /scratch。")
    _audit("get", str(remote_paths)[:300], rc, time.time() - t0,
           {"bytes": total, "files": len(got)})
    return rc, out or f"取回 {len(got)} 个文件 / {total/1e6:.2f} MB"


def node_of(job):
    rc, out = light(f'squeue -j {job} -h -o "%N"')
    return out.strip() if rc == 0 and out.strip() else None


def job_state(job):
    rc, out = light(f'squeue -j {job} -h -o "%T"')
    if rc != 0:
        return None
    return out.strip().upper() or "GONE"


def node_healthy(job):
    """健康 = 作业在跑 且 它当前所在节点无 down/drain 标记。

    必须按**作业当前所在节点**判断，不能写死节点名：2026-08-08 作业被 requeue
    到另一张 A100 后，写死 icsnode06 的看门狗一直盯着无关的机器，永不触发。
    """
    st = job_state(job)
    if st != "RUNNING":
        return False, f"作业 {st}"
    node = node_of(job)
    if not node:
        return False, "取不到作业所在节点"
    rc, out = light(f'sinfo -N -n {node} -h -o "%T"')
    if rc != 0:
        return False, "sinfo 探测失败"
    state = out.split()[0] if out else ""
    flagged = any(c in state for c in "*~#$")
    ok = state.strip("*~#$").lower() in HEALTHY and not flagged
    return ok, f"{node}={state}"


def _acquire():
    """全局并发闸。基于 O_EXCL 原子创建，跨进程有效。"""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release():
    try:
        os.remove(LOCK)
    except OSError:
        pass


def heavy(job, script, timeout=3600, label="", require_healthy=True):
    """远程重操作（下载/渲染/训练）。占用全局并发闸。

    返回 (rc, out)。rc=-3 表示被闸挡下（已有重操作在跑），rc=-4 表示节点不健康。
    **被挡下时不重试**——重操作在机器生病时重试只会加剧问题。
    """
    if require_healthy:
        ok, why = node_healthy(job)
        if not ok:
            _audit("heavy-refused", script, -4, 0.0, {"reason": why, "label": label})
            return -4, f"拒绝执行：节点/作业不健康（{why}）"

    if not _acquire():
        holder = ""
        try:
            holder = open(LOCK, encoding="utf-8").read().strip()
        except Exception:
            pass
        _audit("heavy-blocked", script, -3, 0.0, {"holder_pid": holder, "label": label})
        return -3, f"拒绝执行：已有远程重操作在跑（pid={holder}）"

    try:
        quoted = "'" + script.replace("'", "'\\''") + "'"
        rc, out, secs = _run(f"srun --jobid={job} --overlap bash -lc {quoted}", timeout)
        _audit("heavy", script, rc, secs, {"label": label})
        return rc, out
    finally:
        _release()


def backoff_sleep(consecutive_failures):
    """故障期指数退避。定频重试等于在机器生病时持续加压。"""
    d = min(BACKOFF_BASE * (2 ** max(0, consecutive_failures - 1)), BACKOFF_MAX)
    time.sleep(d)
    return d


if __name__ == "__main__":
    import sys
    job = sys.argv[1] if len(sys.argv) > 1 else "78208"
    ok, why = node_healthy(job)
    print(f"健康={ok}  ({why})")
    print(f"并发闸={'占用中' if os.path.exists(LOCK) else '空闲'}  审计日志={AUDIT}")
    sys.exit(0 if ok else 1)
