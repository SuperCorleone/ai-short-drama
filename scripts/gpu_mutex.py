"""本机 4070（8GB / 可用约 5.8GB）的 GPU 互斥锁。

同属 2026-08-08 事故的「聚合约束」类问题：项目一直有"ComfyUI / TTS / L1 eval
三者必须串行错峰"的纪律，但**没有任何强制**，全靠我每次手动 `netstat` 查一眼。
rosa 那次证明了"靠记得"不可靠——同一天我刚写完并发纪律就把并发放大了 5 倍。

所以本机也做成代码强制：谁要用 GPU，先拿锁。

用法:
    python scripts/gpu_mutex.py status
    python scripts/gpu_mutex.py acquire comfyui   # 拿不到会告诉你被谁占着
    python scripts/gpu_mutex.py release comfyui
    python scripts/gpu_mutex.py run comfyui -- <命令...>   # 自动拿锁-执行-释放

设计取舍：
  - 用**文件锁 + 端口探测双重判定**。只靠端口会漏掉不监听端口的进程（如批处理脚本）；
    只靠文件锁会被残留锁卡死（进程崩溃没释放）
  - 残留锁自动回收：锁里记 PID，PID 不存在就判定为残留并接管
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "pipeline", ".gpu.lock")

# 已知占卡服务与其端口，用于双重判定
SERVICES = {"comfyui": 8189, "tts": 8390, "eval": 8391}


def _port_listening(port):
    """端口是否**正在被监听**。

    🐛 初版写成 `f":{port} " in stdout and "LISTENING" in stdout` —— 两个子串
    各自在**整份输出**里找，不要求同一行。系统里只要有任何别的端口在 LISTENING，
    第二个条件就恒成立；于是 ComfyUI 退出后残留的 `TIME_WAIT` 连接
    （本机实测 22 条，全是 TIME_WAIT、零 LISTENING）就足以让它误判为"仍在监听"。
    后果是每次 ComfyUI 退出后的约 4 分钟内，这道护栏都会拦下合法操作 ——
    **误报的护栏会教人学会绕过护栏**，那比没有护栏更糟。

    改为逐行判定：本地地址必须以 `:port` 结尾，且该行状态为 LISTENING。
    """
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    for line in out.stdout.splitlines():
        f = line.split()
        # 形如: TCP  127.0.0.1:8189  0.0.0.0:0  LISTENING  1234
        if len(f) >= 4 and f[0].upper() in ("TCP", "TCPV6") \
                and f[1].endswith(f":{port}") and f[3].upper() == "LISTENING":
            return True
    return False


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, timeout=30)
        return str(pid) in out.stdout
    except Exception:
        return True   # 查不到就当活着，宁可保守


def read_lock():
    if not os.path.exists(LOCK):
        return None
    try:
        return json.load(open(LOCK, encoding="utf-8"))
    except Exception:
        return {"owner": "?", "pid": 0, "ts": 0}


def status():
    cur = read_lock()
    stale = bool(cur and cur.get("pid") and not _pid_alive(cur["pid"]))
    print(f"GPU 锁: {'空闲' if not cur else cur['owner'] + (' (残留)' if stale else '')}")
    for name, port in SERVICES.items():
        print(f"  {name:8s} :{port}  {'LISTENING' if _port_listening(port) else '-'}")
    return 0


def acquire(owner, wait=0):
    deadline = time.time() + wait
    while True:
        cur = read_lock()
        if cur and cur.get("pid") and not _pid_alive(cur["pid"]):
            print(f"[info] 回收残留锁（原持有者 {cur['owner']} pid={cur['pid']} 已不存在）")
            release(cur["owner"], force=True)
            cur = None
        if not cur:
            os.makedirs(os.path.dirname(LOCK), exist_ok=True)
            try:
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps(
                    {"owner": owner, "pid": os.getppid(), "ts": time.time()}).encode())
                os.close(fd)
                # 端口双重判定：锁拿到了，但别的占卡服务还在监听 → 让出并提示
                busy = [n for n, p in SERVICES.items() if n != owner and _port_listening(p)]
                if busy:
                    release(owner, force=True)
                    print(f"[abort] 锁可得，但 {busy} 仍在监听端口（占着显存）。"
                          f"8GB 卡放不下并发，请先停掉它们。")
                    return 2
                print(f"[ok] {owner} 已获得 GPU 锁")
                return 0
            except FileExistsError:
                cur = read_lock()
        if time.time() >= deadline:
            print(f"[abort] GPU 被 {cur.get('owner')} 占用（pid={cur.get('pid')}）。"
                  f"8GB 卡必须串行错峰——先释放再用。")
            return 1
        time.sleep(5)


def release(owner, force=False):
    cur = read_lock()
    if not cur:
        return 0
    if not force and cur.get("owner") != owner:
        print(f"[abort] 锁属于 {cur.get('owner')}，不是 {owner}，拒绝释放")
        return 1
    try:
        os.remove(LOCK)
    except OSError:
        pass
    if not force:
        print(f"[ok] {owner} 已释放 GPU 锁")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "acquire", "release", "run"])
    ap.add_argument("owner", nargs="?", default="")
    ap.add_argument("--wait", type=int, default=0, help="拿不到锁时等待秒数")
    ap.add_argument("cmd", nargs="*", help="run 模式下要执行的命令（放在 -- 之后）")
    args = ap.parse_args()

    if args.action == "status":
        return status()
    if not args.owner:
        ap.error("acquire/release/run 需要 owner（comfyui/tts/eval/其他名字）")
    if args.action == "acquire":
        return acquire(args.owner, args.wait)
    if args.action == "release":
        return release(args.owner)

    cmd = [c for c in args.cmd if c != "--"]
    if not cmd:
        ap.error("run 模式需要在 -- 之后给出命令")
    rc = acquire(args.owner, args.wait)
    if rc != 0:
        return rc
    try:
        return subprocess.run(cmd).returncode
    finally:
        release(args.owner)


if __name__ == "__main__":
    sys.exit(main())
