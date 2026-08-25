#!/usr/bin/env python3
"""
rerender_frames.py — 用当前量化代码重渲染已缓存的 FPS6 帧。

适用场景：显示管线修复/调参（如 ADR-0004 墨水对混色、浓彩偏置调整）后，
已缓存的帧仍是旧代码产物，屏上内容不对。本脚本从各处的「原图」（.orig，
ADR-0001：与帧同生同灭）重新量化，原地覆盖帧文件；不重新生成内容
（不调 LLM / 文生图）。

覆盖范围：
  1. 自由模块当日缓存 free_cache/free_*.fps6（有 .libid 边车且原图在库）
  2. 内容库 uploads/*.fps6（存在同名 .orig）

重渲染后字节变化 → 内容指纹变化 → 设备下次轮询自动刷屏，无需手动推送。
注意：服务进程有 12h 内存缓存，跑完后需重启服务（launchctl kickstart）。

用法：
    python3 tools/rerender_frames.py           # 重渲染全部
    python3 tools/rerender_frames.py --dry     # 只看会动哪些文件
"""
import argparse
import sys
from pathlib import Path

_SERVICES = Path(__file__).resolve().parent.parent / "services"
sys.path.insert(0, str(_SERVICES))
# upload_dir 等默认值是 CWD 相对路径，服务以 services/ 为工作目录运行；
# 脚本无论从哪里发起都以 services/ 为基准解析
import os  # noqa: E402
os.chdir(_SERVICES)

from app.config import settings  # noqa: E402
from app.epd_image import prepare_image  # noqa: E402


def rerender(orig: Path, frame: Path, dry: bool) -> bool:
    """原图 -> 新 FPS6 字节，写回 frame。返回是否实际重写。"""
    if not orig.is_file():
        print(f"  跳过 {frame.name}：原图缺失")
        return False
    prepared = prepare_image(orig.read_bytes(), dither=True)
    new = prepared.data
    old = frame.read_bytes() if frame.is_file() else b""
    if new == old:
        print(f"  不变 {frame.name}")
        return False
    if not dry:
        frame.write_bytes(new)
    print(f"  重渲染 {frame.name} ({len(old)} -> {len(new)} bytes)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry", action="store_true", help="只列出动作，不写文件")
    args = ap.parse_args()

    uploads = Path(settings.upload_dir)
    free_cache = Path(settings.free_cache_dir)
    changed = 0

    print("== 自由模块缓存 ==")
    for sidecar in sorted(free_cache.glob("free_*.libid")):
        frame = sidecar.with_suffix(".fps6")
        lib_id = sidecar.read_text().strip()
        orig = uploads / f"{lib_id}.orig"
        changed += rerender(orig, frame, args.dry)

    print("== 内容库 ==")
    for frame in sorted(uploads.glob("*.fps6")):
        orig = frame.with_suffix(".orig")
        if not orig.is_file():
            continue
        changed += rerender(orig, frame, args.dry)

    print(f"\n{'将重渲染' if args.dry else '已重渲染'} {changed} 个帧"
          + ("（dry run，未写入）" if args.dry else ""))
    if changed and not args.dry:
        print("记得重启服务使内存缓存失效：launchctl kickstart -k gui/$(id -u)/com.framedphoto.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
