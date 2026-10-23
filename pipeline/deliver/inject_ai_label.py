#!/usr/bin/env python3
"""P1.5b · AI 生成内容标识三处注入（片头前 3s 角标 + 封面 + 简介）。

## 这不是平台的可选项，是法定义务

国家《人工智能生成合成内容标识办法》**2025-09-01 起强制**：AI 生成占比 ≥50%
的内容必须显式标识。抖音《AI 短剧审片标准》把落点定死在三处 ——
**片头前 3 秒 + 封面 + 简介**。三处缺一即违规。

所以 `eval/gate.py` 把它划进 **L0 硬性项**：缺标识一律 REJECT 而不是 RETRY。
道理很直白 —— "忘了加 AI 标识"是装配环节的错，重抽多少次画面都不会长出角标来。

## 为什么要写 sidecar

`gate.py` 的 `--ai-label` 原本是**人手拨的开关**。人手拨的开关，在一条要跑几十集的
流水线上等价于迟早会拨错，而且错的方向必然是"标成已加"（因为那样门禁才放行）。
本脚本注入成功后落一份 `<输出名>.ailabel.json`，让门禁去读**事实**而不是读声明。

## 角标为什么是这个样子

- **位置右上**：竖屏信息流里左上常被平台自己的头像/昵称浮层占据
- **半透明黑底 + 白字**：纯文字在亮背景上会糊掉；描边在小屏上发虚，垫底色最稳
- **只在前 3 秒显示**（`enable='lt(t,3)'`）：法规要求的是"起始画面显著位置"，
  全程挂角标反而影响观感，也不是规定所要求的
- **字号随画面宽度缩放**：写死像素值在 1080×1920 上合适，换个分辨率就会失真

用法:
    python pipeline/deliver/inject_ai_label.py --video ep01.mp4 --out ep01_labeled.mp4
    python pipeline/deliver/inject_ai_label.py --video ep01.mp4 --out ep01_labeled.mp4 \\
        --cover cover.png --desc-in 简介.txt --desc-out 简介_labeled.txt
    python pipeline/deliver/inject_ai_label.py --self-test
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

LABEL_TEXT = "AI生成"
# 简介里的标识语。用词贴合监管表述（"生成合成"是《标识办法》的原词）。
DESC_NOTICE = "【本内容由人工智能生成合成】"

# Windows 中文字体。drawtext 的 fontfile 在 Windows 上要把盘符冒号转义成 `C\:/...`，
# 否则 ffmpeg 会把冒号当成 filter 参数分隔符 —— 这是个只在 Windows 上出现的坑。
FONT_CANDIDATES = [
    r"C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑 Bold，小尺寸下最清晰
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
]


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def escape_fontfile(path):
    """ffmpeg filter 里的路径转义：盘符冒号必须转义，反斜杠一律转正斜杠。"""
    return path.replace("\\", "/").replace(":", r"\:")


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", path],
        capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {out.stderr[:300]}")
    data = json.loads(out.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not v:
        raise RuntimeError("没有视频流")
    return {"width": int(v["width"]), "height": int(v["height"]),
            "duration_s": float(data["format"].get("duration") or 0),
            "has_audio": any(s["codec_type"] == "audio" for s in data["streams"])}


def badge_filter(width, seconds=3.0, text=LABEL_TEXT):
    """生成前 N 秒右上角标的 filter 串。字号与边距按画面宽度等比缩放。"""
    font = find_font()
    if not font:
        raise RuntimeError(f"找不到中文字体，试过: {FONT_CANDIDATES}")
    fs = max(18, round(width * 0.045))     # 1080 宽 → 约 49px
    pad = max(6, round(width * 0.012))
    margin = max(10, round(width * 0.028))
    # box=1 垫半透明底；enable 限定只在前 seconds 秒出现
    return (
        f"drawtext=fontfile='{escape_fontfile(font)}'"
        f":text='{text}':fontcolor=white:fontsize={fs}"
        f":box=1:boxcolor=black@0.55:boxborderw={pad}"
        f":x=w-tw-{margin}:y={margin}"
        f":enable='lt(t,{seconds})'"
    )


def inject_video(src, dst, seconds=3.0, text=LABEL_TEXT):
    spec = probe(src)
    vf = badge_filter(spec["width"], seconds, text)
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p"]
    # 有音轨就原样复制，别重编码 —— 重编码会动响度，而 -14 LUFS 是 L0 硬性项，
    # 在标识注入这一步把它改掉，门禁会在下游报一个跟标识毫无关系的错。
    cmd += ["-c:a", "copy"] if spec["has_audio"] else ["-an"]
    cmd += [dst]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 注入角标失败: {r.stderr[-600:]}")
    return spec


def make_cover(src, dst, at_s=0.5, text=LABEL_TEXT):
    """从成片抽一帧做封面，并把同款角标烧进去。

    封面单独烧一次而不是直接截取已加角标的第 1 帧：封面的角标应当**常驻**，
    而视频角标只在前 3 秒出现；两者的生命周期不同，不能共用一次渲染。
    """
    spec = probe(src)
    font = find_font()
    fs = max(18, round(spec["width"] * 0.045))
    pad = max(6, round(spec["width"] * 0.012))
    margin = max(10, round(spec["width"] * 0.028))
    vf = (f"drawtext=fontfile='{escape_fontfile(font)}'"
          f":text='{text}':fontcolor=white:fontsize={fs}"
          f":box=1:boxcolor=black@0.55:boxborderw={pad}"
          f":x=w-tw-{margin}:y={margin}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(at_s), "-i", src, "-vf", vf,
         "-frames:v", "1", dst],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 生成封面失败: {r.stderr[-600:]}")
    return dst


def inject_desc(text_in, path_out, notice=DESC_NOTICE):
    """简介加标识。放在**开头**——信息流里简介常被折叠，放末尾会被折掉。"""
    body = ""
    if text_in and os.path.exists(text_in):
        body = open(text_in, encoding="utf-8").read().strip()
    elif text_in:
        body = text_in.strip()
    content = notice + ("\n" + body if body else "")
    with open(path_out, "w", encoding="utf-8") as f:
        f.write(content)
    return content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video")
    ap.add_argument("--out")
    ap.add_argument("--cover", help="封面输出路径（默认 <out>.cover.png）")
    ap.add_argument("--desc-in", default="", help="原简介文件路径或直接给正文")
    ap.add_argument("--desc-out", help="简介输出路径（默认 <out>.desc.txt）")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--text", default=LABEL_TEXT)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.video or not args.out:
        ap.error("需要 --video 与 --out（或 --self-test）")

    cover = args.cover or (os.path.splitext(args.out)[0] + ".cover.png")
    desc_out = args.desc_out or (os.path.splitext(args.out)[0] + ".desc.txt")

    spec = inject_video(args.video, args.out, args.seconds, args.text)
    make_cover(args.out, cover, text=args.text)
    desc = inject_desc(args.desc_in, desc_out, DESC_NOTICE)

    sidecar = args.out + ".ailabel.json"
    record = {
        "video": os.path.abspath(args.out),
        "badge": {"text": args.text, "seconds": args.seconds, "position": "top-right"},
        "cover": os.path.abspath(cover),
        "desc": os.path.abspath(desc_out),
        "desc_notice": DESC_NOTICE,
        "source_spec": spec,
        "all_three_present": all(os.path.exists(p) for p in (args.out, cover, desc_out)),
        "basis": "国家《人工智能生成合成内容标识办法》2025-09-01 + 抖音《AI 短剧审片标准》",
    }
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    print(f"角标  -> {args.out}（前 {args.seconds}s，右上）")
    print(f"封面  -> {cover}")
    print(f"简介  -> {desc_out}  首行: {desc.splitlines()[0]}")
    print(f"凭据  -> {sidecar}  三处齐全={record['all_three_present']}")
    print(f"\n门禁: eval/gate.py 读这份凭据判 --ai-label，不要手工拨开关")
    return 0 if record["all_three_present"] else 1


def _self_test():
    """自检：造一段测试视频，注入后**逐项核验产物**，而不是只看 ffmpeg 返回 0。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ailabel_")
    src = os.path.join(tmp, "src.mp4")
    dst = os.path.join(tmp, "out.mp4")
    cases = []

    try:
        # 5 秒 1080x1920 竖屏测试片 + 静音音轨（要验证音轨被 copy 而非丢弃）
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=24:duration=5",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src],
            capture_output=True, text=True, timeout=300)
        cases.append(("造测试片", r.returncode == 0, r.stderr[-200:] if r.returncode else ""))

        cases.append(("找到中文字体", find_font() is not None, find_font()))

        spec = inject_video(src, dst)
        cases.append(("注入角标产出文件", os.path.exists(dst) and os.path.getsize(dst) > 0, ""))

        out_spec = probe(dst)
        cases.append(("分辨率不变", (out_spec["width"], out_spec["height"]) == (1080, 1920),
                      out_spec))
        cases.append(("时长不变（±0.2s）", abs(out_spec["duration_s"] - spec["duration_s"]) < 0.2,
                      f"{spec['duration_s']} -> {out_spec['duration_s']}"))
        cases.append(("音轨保留", out_spec["has_audio"] is True, out_spec["has_audio"]))

        # 角标真的画上去了吗？比较第 1 秒（应有角标）与第 4 秒（应无）右上角区域的像素。
        # 只看 ffmpeg 退出码是不够的 —— filter 写错但语法合法时它照样返回 0。
        def corner_stats(t):
            p = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(t), "-i", dst,
                 "-vf", "crop=300:120:780:20", "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                capture_output=True, timeout=120)
            data = p.stdout
            return (sum(data) / len(data)) if data else -1

        early, late = corner_stats(1.0), corner_stats(4.0)
        cases.append(("前 3s 右上角有角标（与 4s 处像素不同）",
                      early >= 0 and late >= 0 and abs(early - late) > 3.0,
                      f"t=1s 均值 {early:.1f} / t=4s 均值 {late:.1f}"))

        cover = os.path.join(tmp, "cover.png")
        make_cover(dst, cover)
        cases.append(("封面生成", os.path.exists(cover) and os.path.getsize(cover) > 0, ""))

        desc_out = os.path.join(tmp, "desc.txt")
        body = inject_desc("这是一集测试短剧。", desc_out)
        cases.append(("简介标识在首行", body.startswith(DESC_NOTICE), body.splitlines()[0]))
        cases.append(("简介保留原文", "这是一集测试短剧。" in body, ""))

        empty_desc = os.path.join(tmp, "desc2.txt")
        b2 = inject_desc("", empty_desc)
        cases.append(("空简介也能加标识", b2.strip() == DESC_NOTICE, repr(b2)))
    finally:
        pass

    ok = sum(1 for _, c, _ in cases if c)
    for name, cond, detail in cases:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond or detail else ""))
    print(f"\n自检 {ok}/{len(cases)} 通过   临时目录 {tmp}")
    if ok == len(cases):
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("（有失败项，临时目录保留供排查）")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
