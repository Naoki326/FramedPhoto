#!/usr/bin/env python3
"""计算 NAS 同步的平均速度（下载量 ÷ 时间，而非瞬时速度平均）。

从 services/sync_nas.log 的 rsync progress2 行取最近 N 个采样点，
平均速度 = (最后字节 - 最早字节) / (最后时间 - 最早时间)。

用法:
    python tools/sync_speed.py [采样数] [--watch] [--interval 5]

    --watch            循环刷新（Ctrl-C 退出），适合挂着看
    --interval SEC      --watch 时的刷新间隔（默认 5）
"""
import argparse
import re
import sys
import time
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "services" / "sync_nas.log"

# progress2 行形如: "  132,584,874   4%  100.46kB/s    0:21:28 (xfr#14, to-chk=186/358)"
PROG_RE = re.compile(
    r"^\s*([\d,]+)\s+\d+%\s+\S+\s+(\d+(?::\d+){1,2})\s+\(xfr#(\d+)"
)
BYTES_RE = re.compile(r"^([\d,]+)")

def parse_elapsed(s: str) -> float:
    secs = 0.0
    for p in s.split(":"):
        secs = secs * 60 + float(p)
    return secs

def read_samples() -> list[tuple[float, float, int]]:
    """返回 [(elapsed, bytes, xfr#), ...]，仅本次运行（脚本启动时清空日志）。"""
    lines = LOG.read_text(encoding="utf-8", errors="ignore").replace("\r", "\n").split("\n")
    samples: list[tuple[float, float, int]] = []
    for line in lines:
        m = PROG_RE.match(line)
        if not m:
            continue
        by = int(m.group(1).replace(",", ""))
        el = parse_elapsed(m.group(2))
        xfr = int(m.group(3))
        samples.append((el, by, xfr))
    return samples

def fmt_rate(bps: float) -> str:
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.2f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.2f} KB/s"
    return f"{bps:.1f} B/s"

def fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"

def report(n: int) -> None:
    samples = read_samples()
    if len(samples) < 2:
        print(f"[sync_speed] 日志采样不足（{len(samples)} 行，需 ≥2），同步可能尚未开始")
        return
    win = samples[-n:]
    t0, b0, x0 = win[0]
    t1, b1, x1 = win[-1]
    dt = t1 - t0
    db = b1 - b0
    if dt <= 0 or db < 0:
        print("[sync_speed] 采样窗口无效（时间/字节未前进）")
        return
    avg = db / dt
    # 瞬时速度：最后两个样本
    t_a, b_a, _ = samples[-2]
    t_b, b_b, _ = samples[-1]
    inst = (b_b - b_a) / (t_b - t_a) if t_b > t_a and b_b >= b_a else None

    print(f"[sync_speed] 采样窗口: 最近 {len(win)} 行 / {dt:,.0f}s")
    print(f"  窗口起点: {fmt_bytes(b0)} (t={t0:,.0f}s, xfr#{x0})")
    print(f"  窗口终点: {fmt_bytes(b1)} (t={t1:,.0f}s, xfr#{x1})")
    print(f"  下载量差: {fmt_bytes(db)}")
    print(f"  平均速度: {fmt_rate(avg)}  （下载量 ÷ 时间）")
    if inst is not None and inst > 0:
        print(f"  瞬时速度(最近两行): {fmt_rate(inst)}")
    else:
        print(f"  瞬时速度: 最近两行字节未变化（文件传输间隙）")

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("n", nargs="?", type=int, default=10, help="采样点数（默认 10）")
    ap.add_argument("--watch", action="store_true", help="循环刷新")
    ap.add_argument("--interval", type=int, default=5, help="--watch 刷新间隔秒数（默认 5）")
    args = ap.parse_args()
    if args.n < 2:
        sys.exit("采样数需 ≥ 2")

    if not args.watch:
        report(args.n)
        return
    try:
        while True:
            print("\033[2J\033[H", end="")  # 清屏
            report(args.n)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已退出")

if __name__ == "__main__":
    main()
