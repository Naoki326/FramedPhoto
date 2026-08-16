"""
library.py — 内容库：上传图片资产（SQLite 元数据 + upload_dir 文件）。

内容库是唯一「id 是查找键」的内容源（ADR-0002）：无前缀 id 即内容库
图片 id，查库读文件（fps6 帧 / .orig 原图）。其余四种生成源（每日精选/
置顶显示/天气卡片/自由模块）的 id 是内容指纹，不是查找键。

内容库进内容清单走「推送到显示」注入机制（/content 的 pushed 分支，
当日有效），不经源的 meta()——内容库没有「当前内容」概念。
"""
from __future__ import annotations

from pathlib import Path

from app import db
from app.config import settings

UPLOAD_DIR = Path(settings.upload_dir)


class LibrarySource:
    """内容库内容源：注册表的无前缀兜底（表尾，见 content_sources.resolve）。

    id_prefix 为空串：任何未命中其他源前缀的 id 都落内容库按查找键查库。
    """

    id_prefix = ""
    missing_detail = "image not found"

    def meta(self) -> dict | None:
        """内容库没有「当前内容」概念（id 是查找键），恒返回 None。

        仅满足内容源接口形状；清单条目由 /content 的推送分支组装。
        """
        return None

    def render(self, content_id: str) -> bytes | None:
        """按 id 查库读 FPS6 帧；库中无此 id 返回 None（路由层 404）。"""
        meta = db.get_image(content_id)
        if not meta:
            return None
        return (UPLOAD_DIR / meta["fps6_path"]).read_bytes()

    def original(self, content_id: str) -> bytes | None:
        """按 id 读原图（.orig 全彩字节）；id 不在库或原图缺失返回 None。"""
        if not db.get_image(content_id):
            return None
        orig = UPLOAD_DIR / f"{content_id}.orig"
        if orig.is_file():
            try:
                return orig.read_bytes()
            except OSError:
                return None
        return None


SOURCE = LibrarySource()
