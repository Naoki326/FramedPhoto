"""calendar_facts.py — 农历 / 节气 / 节日的共享查询（天气卡片 + 日历卡片共用）。

全部纯本地计算（lunar_python），无网络依赖。标注优先级与天气卡片历史
语义一致：公历节日 > 节气 > 传统节日；月历卡片按返回类型分色
（节日红 / 节气绿，见 calendar_card）。
"""
from __future__ import annotations

import datetime as dt

from lunar_python import Solar

# 重要公历/农历节日（过滤掉观莲节等小众节日）
MAJOR_FESTIVALS = {
    "元旦", "春节", "元宵", "情人节", "妇女节", "植树节", "清明", "劳动节", "青年节",
    "母亲节", "儿童节", "端午", "父亲节", "建党节", "建军节", "七夕", "中元",
    "教师节", "中秋", "国庆", "重阳", "万圣节", "感恩节", "平安夜", "圣诞",
    "腊八", "除夕",
}

# 公历节日（lunar_python 的 getOtherFestivals 只含传统节日，公历的需硬编码）
SOLAR_FESTIVALS = {
    (1, 1): "元旦", (2, 14): "情人节", (3, 8): "妇女节", (3, 12): "植树节",
    (5, 1): "劳动节", (5, 4): "青年节", (6, 1): "儿童节", (7, 1): "建党节",
    (8, 1): "建军节", (9, 10): "教师节", (10, 1): "国庆节", (12, 24): "平安夜",
    (12, 25): "圣诞",
}


def solar_festival(d: dt.date) -> str:
    """公历节日名（元旦 / 情人节 / 国庆节…）；无返回空串。"""
    return SOLAR_FESTIVALS.get((d.month, d.day), "")


def jieqi(d: dt.date) -> str:
    """节气名（立春 / 雨水…）；无或计算失败返回空串。"""
    try:
        return Solar.fromYmd(d.year, d.month, d.day).getLunar().getJieQi() or ""
    except Exception:  # noqa: BLE001
        return ""


def lunar_festival(d: dt.date) -> str:
    """传统节日名（春节 / 端午 / 中秋…，已过滤小众）；无返回空串。"""
    try:
        ll = Solar.fromYmd(d.year, d.month, d.day).getLunar()
    except Exception:  # noqa: BLE001
        return ""
    for f in ll.getFestivals() + ll.getOtherFestivals():
        core = f[:-1] if f.endswith("节") else f
        if f in MAJOR_FESTIVALS:
            return f
        if core in MAJOR_FESTIVALS:
            return core
    return ""


def day_mark(d: dt.date) -> str:
    """当天标注：公历节日 > 节气 > 传统节日；无返回空串。"""
    return solar_festival(d) or jieqi(d) or lunar_festival(d)


def upcoming_marks(today: dt.date, days_ahead: int = 10, max_marks: int = 2) -> tuple[str, str]:
    """→ (今日标注, 未来预告串)。预告如 "立秋 8/7 · 七夕 8/19"。"""
    today_mark = day_mark(today)
    soon: list[str] = []
    for i in range(1, days_ahead + 1):
        d = today + dt.timedelta(days=i)
        m = day_mark(d)
        if m:
            soon.append(f"{m} {d.month}/{d.day}")
        if len(soon) >= max_marks:
            break
    return today_mark, " · ".join(soon)


def lunar_day_label(d: dt.date) -> str:
    """农历小字：初一显示月名（七月 / 闰七月），其余显示日名（初五 / 廿一）。

    月历格子里每天一格小字的经典写法；计算失败返回空串。
    """
    try:
        ll = Solar.fromYmd(d.year, d.month, d.day).getLunar()
        if ll.getDay() == 1:
            return f"{ll.getMonthInChinese()}月"
        return ll.getDayInChinese()
    except Exception:  # noqa: BLE001
        return ""
