"""weather_lookup.py — 和风城市查询（中文城市名 → location ID）。

管理台「城市」输入框保存时前端会先调这里：输入中文城市名（如「上海宝山」）
自动解析成和风 location（城市 ID / 坐标），让天气真正按所选城市取数。
解析失败的兜底见 app/web/index.html 的 CITY_ID_MAP（常用城市内置表）。
"""
import requests
from fastapi import APIRouter

from app.config import settings

router = APIRouter()

# 和风城市查询 API 会请求失败/超时时回退的中文城市库（与前端内置表互补）
_FALLBACK_CITY_IDS = {
    "上海宝山": "101020300", "宝山": "101020300", "嘉定": "101020900",
    "浦东": "101020600", "松江": "101020700", "青浦": "101020800",
}

# GeoAPI 查不到、但天气接口能直接收中文名的常见城市 → 城市 ID
_COMMON_CITY_IDS = {
    "北京": "101010100", "北京市": "101010100",
    "上海": "101020100", "上海市": "101020100",
    "广州": "101280101", "广州市": "101280101",
    "深圳": "101280601", "深圳市": "101280601",
    "杭州": "101210101", "杭州市": "101210101",
    "南京": "101190101", "南京市": "101190101",
    "苏州": "101190401", "苏州市": "101190401",
    "成都": "101270101", "成都市": "101270101",
    "重庆": "101040100", "重庆市": "101040100",
    "武汉": "101200101", "武汉市": "101200101",
    "西安": "101110101", "西安市": "101110101",
    "天津": "101030100", "天津市": "101030100",
}


@router.get("/lookup")
async def lookup_city(location: str):
    """中文城市名 → 和风 location 列表 [{name, id, adm1, lat, lon}]。"""
    name = (location or "").strip()
    if not name:
        return {"location": []}

    # 1) 内置常用城市表（离线可用，最稳）
    if name in _FALLBACK_CITY_IDS:
        return {"location": [{"name": name, "id": _FALLBACK_CITY_IDS[name], "adm1": "上海"}]}

    # 2) 和风 GeoAPI 城市查询（可能 404 / 超时 / 未开通）
    if settings.qweather_key:
        try:
            r = requests.get(
                "https://geoapi.qweather.com/v2/city/lookup",
                params={"location": name, "key": settings.qweather_key},
                timeout=8,
            )
            data = r.json()
            locs = data.get("location") or []
            if locs:
                return {"location": [{
                    "name": l.get("name", ""),
                    "id": l.get("id", ""),
                    "adm1": l.get("adm1", ""),
                    "lat": l.get("lat", ""),
                    "lon": l.get("lon", ""),
                } for l in locs[:5]]}
        except Exception:  # noqa: BLE001  网络失败静默，走兜底
            pass

    # 3) 常见城市名兜底（映射到城市 ID，天气接口可靠）
    if name in _COMMON_CITY_IDS:
        return {"location": [{"name": name, "id": _COMMON_CITY_IDS[name],
                              "adm1": "", "lat": "", "lon": ""}]}

    # 4) 和风天气 API 直接试中文（个别城市可用）
    if settings.qweather_key:
        try:
            from app.weather_card import QWEATHER_BASE
            base = QWEATHER_BASE.format(host=settings.qweather_host)
            r = requests.get(f"{base}/now",
                             params={"location": name, "key": settings.qweather_key}, timeout=8)
            d = r.json()
            if d.get("code") == "200":
                return {"location": [{
                    "name": name, "id": name,
                    "adm1": "", "lat": "", "lon": "",
                }]}
        except Exception:  # noqa: BLE001
            pass

    return {"location": []}
