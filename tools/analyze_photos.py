#!/usr/bin/env python3
"""
analyze_photos.py — 扫描照片库，AI 分析并评分入库。

用法：
    python tools/analyze_photos.py /path/to/photos [--no-vlm] [-j 4] [--debug]

行为：
  - 扫描目录下常见图片格式（jpg/png/heic/webp/bmp）
  - 逐张调用 VLM（配置见 services/.env）或启发式评分
  - 结果写入 SQLite（photo_scores 表），已分析过的照片跳过（断点续跑）
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 让脚本能 import 服务端模块，并统一工作目录（db/upload 路径一致）
_SERVICES = Path(__file__).resolve().parent.parent / "services"
sys.path.insert(0, str(_SERVICES))
os.chdir(_SERVICES)

from app import db  # noqa: E402
from app.analyzer import analyze_image  # noqa: E402

SUPPORTED = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tiff"}


def collect_images(root: Path) -> list[Path]:
    files = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED:
            files.append(p)
    return sorted(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("photo_dir", help="照片库目录")
    ap.add_argument("--no-vlm", action="store_true", help="强制使用启发式评分（不调用 VLM）")
    ap.add_argument("-j", "--concurrency", type=int, default=1, help="并发线程数")
    args = ap.parse_args()

    root = Path(args.photo_dir)
    if not root.is_dir():
        print(f"目录不存在: {root}")
        return 1

    images = collect_images(root)
    if not images:
        print("没有找到图片")
        return 1

    # 断点续跑：跳过已分析
    pending = []
    for p in images:
        row = db.get_photo_score(str(p))
        if not row or row.get("analyzed_at") is None:
            pending.append(p)
    print(f"共 {len(images)} 张，待分析 {len(pending)} 张（并发 {args.concurrency}）")

    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(analyze_image, str(p), use_vlm=not args.no_vlm): p for p in pending}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                a = fut.result()
            except Exception as exc:  # 单张失败不阻塞
                print(f"  失败 {p.name}: {exc}")
                continue
            db.upsert_photo_score(
                a.path, filename=a.filename, caption=a.caption,
                description=a.description, type=a.type,
                memory_score=a.memory_score, beauty_score=a.beauty_score,
                reason=a.reason, shot_at=a.shot_at, shot_source=a.shot_source,
                gps_lat=a.gps_lat, gps_lon=a.gps_lon,
                source=a.source, analyzed_at=db.now(),
            )
            done += 1
            if done % 10 == 0 or done == len(pending):
                el = time.time() - t0
                print(f"  进度 {done}/{len(pending)}，耗时 {el:.0f}s")

    print(f"完成：{done} 张（{time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
