"""content_sources.py — 内容源注册表（ADR-0002）。

内容源（content source）是一种内容的渲染命名空间：id 前缀标识归属，
独占该 id 到 FPS6 帧与原图的渲染知识。本模块只做一件事：有序前缀表 → 源，
id 命中某前缀即分发到对应源；表尾的内容库源以空前缀兜底（id 是查找键）。

六种源全部在此注册：置顶显示、每日精选、天气卡片、日历卡片、自由模块
（news- 旧前缀作第二前缀）+ 内容库兜底。生成源的 id 是内容指纹（前缀-
渲染字节哈希前 10 位），仅供设备变更检测，不是查找键：源一律服务
「当前内容」，不按指纹寻址。源不负责 url/preview_url 的组装（那是
路由层 helper 的事，见 ADR-0002）。
"""
from __future__ import annotations

from typing import Protocol


class ContentSource(Protocol):
    """内容源接口：id 前缀 + 元数据 / 渲染 / 原图三方法的形状（ADR-0002）。

    meta()：确保当前内容存在，返回内容清单字段（指纹 id、文件名、文案、
        日期、尺寸、横竖屏）；无当前内容返回 None。
    render(content_id)：当前内容的 FPS6 帧；不可用返回 None（路由层 404）。
    original(content_id)：当前内容的原图字节（承 ADR-0001）；缺失返回 None。
    missing_detail：render/original 无当前内容时路由层的 404 文案
        （保持各源改道前的既有语义）。
    """

    id_prefix: str
    missing_detail: str

    def meta(self) -> dict | None: ...
    def render(self, content_id: str) -> bytes | None: ...
    def original(self, content_id: str) -> bytes | None: ...


def resolve(content_id: str) -> ContentSource:
    """有序前缀表查源：首个命中前缀的源；无前缀命中表尾的内容库兜底。"""
    for prefix, source in _ordered_table():
        if content_id.startswith(prefix):
            return source
    raise AssertionError("表尾空前缀必兜底，不可达")


def _ordered_table() -> list[tuple[str, ContentSource]]:
    """有序前缀表（懒加载领域模块，注册表按表分发、不持其他知识）。"""
    from app import calendar_card, daily, display, free_module, library, weather_card
    return [
        (display.SOURCE.id_prefix, display.SOURCE),
        (daily.SOURCE.id_prefix, daily.SOURCE),
        (weather_card.SOURCE.id_prefix, weather_card.SOURCE),
        (calendar_card.SOURCE.id_prefix, calendar_card.SOURCE),
        (free_module.SOURCE.id_prefix, free_module.SOURCE),
        # 旧固件按 id 拼 URL 的兜底：自由模块旧称 news- 作为第二前缀（ADR-0002）
        ("news-", free_module.SOURCE),
        # 无前缀兜底：内容库 id 是查找键（查库读文件），空前缀必须排在表尾
        (library.SOURCE.id_prefix, library.SOURCE),
    ]
