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

from app import category, db  # noqa: E402
from app.analyzer import analyze_image  # noqa: E402

SUPPORTED = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tiff"}


def _refresh_category_record(path: str) -> None:
    """刷新单张照片的分类归属（按 path 第一段推导），保留已有评分；
    无记录则建一条待分析占位（分类先就位）。不触发重新 VLM 分析。"""
    row = db.get_photo_score(path)
    cat = category.derive_category(path)
    if row is None:
        db.upsert_photo_score(path, filename=os.path.basename(path), category=cat)
    else:
        db.upsert_photo_score(path, category=cat)


def collect_images(root: Path) -> list[Path]:
    files = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED:
            files.append(p)
    return sorted(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("photo_dir", help="照片库目录（如 services/photos）")
    ap.add_argument("--no-vlm", action="store_true", help="强制使用启发式评分（不调用 VLM）")
    ap.add_argument("-j", "--concurrency", type=int, default=1, help="并发线程数")
    ap.add_argument("--prune-stale", action="store_true",
                    help="清理数据库中文件已不存在的失效记录（照片删除后）")
    args = ap.parse_args()

    if args.prune_stale:
        n = 0
        for row in db.list_photo_scores(limit=100000):
            if not Path(row["path"]).exists():
                db.upsert_photo_score(row["path"], analyzed_at=None)
                n += 1
        print(f"已清理 {n} 条失效记录（analyzed_at 置空，下次扫描将重新分析）")

    root = Path(args.photo_dir)
    if not root.is_dir():
        print(f"[错误] 照片库目录不可达: {root}")
        print("  请检查 PHOTO_LIB_DIR / 传入路径是否正确（照片库在管理台上传维护）。")
        return 1

    images = collect_images(root)
    if not images:
        print("没有找到图片")
        return 1

    # 断点续跑：跳过已分析；但分类归属是「由 path 第一段推导」的另一维度，
    # 与是否已分析无关——所有扫描到的照片都刷新一次分类（含历史记录回填），
    # 不触发重新 VLM 分析（ADR-0005 分类推导与评分解耦）。
    for p in images:
        _refresh_category_record(str(p))

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
            # 内容哈希迁移：移动（旧路径文件不存在）→ 迁移保留评分；
            # 复制 → 并存两条记录；不命中 → 正常新分析（ADR-0005）
            category.migrate_or_record(a)
            done += 1
            if done % 10 == 0 or done == len(pending):
                el = time.time() - t0
                print(f"  进度 {done}/{len(pending)}，耗时 {el:.0f}s")

    print(f"完成：{done} 张（{time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
