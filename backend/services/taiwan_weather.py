"""臺灣縣市／鄉鎮搜尋與中央氣象署短期格點預報；無定位、DB 或快取。"""
import ast
import asyncio
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from domain.runtime_context import local_time, result

_REGIONS = json.loads((Path(__file__).resolve().parents[1] / "domain/taiwan_regions.json").read_text(encoding="utf-8"))["counties"]


def normalize_region(value: str) -> str:
    return re.sub(r"[\s,，、/]", "", value).replace("台", "臺").removeprefix("臺灣").casefold()


def search_regions(query: str) -> list[dict]:
    query = normalize_region(query)
    if not query:
        return []
    counties = [county for county in _REGIONS if query in {
        normalize_region(county["name"]), normalize_region(county["name"][:-1]),
        county["english"].casefold().replace(" ", ""),
        county["english"].casefold().removesuffix(" city").removesuffix(" county").replace(" ", ""),
    }]
    if counties:
        return [{"county": c["name"], "county_id": c["id"], "town_id": c["default_town"],
                 "location": c["name"], "representative_town": next(
                     name for name, tid in c["towns"].items() if tid == c["default_town"])}
                for c in counties]
    matches = []
    for county in _REGIONS:
        for town, tid in county["towns"].items():
            names = {town, town[:-1], county["name"] + town, county["name"][:-1] + town,
                     county["name"] + town[:-1], county["name"][:-1] + town[:-1]}
            if query in {normalize_region(name) for name in names}:
                matches.append({"county": county["name"], "county_id": county["id"],
                                "town_id": tid, "location": county["name"] + town})
    return matches


def grounded_location(location: str, user_context: list[str]) -> str | None:
    """以真實 user 地名約束模型參數；不能替同名行政區擅自補縣市。"""
    desired = {row["town_id"] for row in search_regions(location)}
    aliases = set()
    for county in _REGIONS:
        aliases.update((county["name"], county["name"][:-1], county["english"],
                        county["english"].removesuffix(" City").removesuffix(" County")))
        for town in county["towns"]:
            aliases.update((town, county["name"] + town, county["name"][:-1] + town,
                            county["name"] + town[:-1], county["name"][:-1] + town[:-1]))
            if len(town[:-1]) >= 2:
                aliases.add(town[:-1])
    for index in range(len(user_context) - 1, -1, -1):
        text = normalize_region(user_context[index])
        mentioned = [alias for alias in aliases if normalize_region(alias) in text]
        if not mentioned:
            continue
        length = max(len(normalize_region(alias)) for alias in mentioned)
        queries = sorted(alias for alias in mentioned if len(normalize_region(alias)) == length)
        for query in queries:
            matches = search_regions(query)
            if not desired.intersection(row["town_id"] for row in matches):
                continue
            if len(matches) > 1 and all("representative_town" not in row for row in matches):
                # 鄉鎮同名時，只能使用最近一則有明確、唯一縣市的 user 情境。
                for previous in reversed(user_context[:index + 1]):
                    normalized = normalize_region(previous)
                    counties = [county for county in _REGIONS if normalize_region(county["name"]) in normalized
                                or normalize_region(county["name"][:-1]) in normalized]
                    if counties:
                        if len(counties) == 1:
                            selected = [row for row in matches if row["county_id"] == counties[0]["id"]]
                            if len(selected) == 1:
                                return selected[0]["location"]
                        break
            return query
        return None
    return None


def js_literal(text: str, variable: str):
    """只解析指定資料 literal，絕不執行來源 JavaScript。"""
    if len(text) > 512 * 1024:
        raise ValueError("source_size")
    match = re.search(r"var\s+" + re.escape(variable) + r"\s*=\s*(.*?);\s*(?:\n|$)", text, re.S)
    if not match:
        raise ValueError("source_format")
    return ast.literal_eval(match.group(1))


def parse_forecast(text: str, region: dict, requested_date: str | None, now: datetime) -> dict:
    updated = re.search(r"Updated:\s*(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", text)
    if not updated:
        raise ValueError("source_format")
    published = datetime.strptime(updated.group(1), "%Y/%m/%d %H:%M:%S").replace(tzinfo=now.tzinfo)
    if not -300 <= (now - published).total_seconds() <= 24 * 3600:
        return result("unavailable", reason="stale_weather")
    labels = js_literal(text, "Time_3hr")["C"]
    values = js_literal(text, "TempArray_3hr")[region["town_id"]]
    times = []
    for label in labels:
        match = re.match(r"(\d{2}) (\d{2})/(\d{2})<", label)
        if not match:
            raise ValueError("source_format")
        hour, month, day = map(int, match.groups())
        choices = []
        for year in (published.year - 1, published.year, published.year + 1):
            try:
                choices.append(datetime(year, month, day, hour, tzinfo=now.tzinfo))
            except ValueError:
                pass
        if not choices:
            raise ValueError("source_format")
        times.append(min(choices, key=lambda date: abs((date - published).total_seconds())))
    if requested_date:
        indices = [i for i, date in enumerate(times) if date.date().isoformat() == requested_date
                   and date.hour in {9, 12, 18} and date >= now.replace(minute=0, second=0, microsecond=0)]
    else:
        start = min(range(len(times)), key=lambda i: abs((times[i] - now).total_seconds()))
        if abs((times[start] - now).total_seconds()) > 3 * 3600:
            return result("unavailable", reason="forecast_out_of_range")
        indices = list(range(start, min(start + 3, len(times))))
    if not indices:
        return result("unavailable", reason="forecast_out_of_range")
    forecast = []
    for index in indices[:3]:
        temperature, apparent = values["C"]["T"][index], values["C"]["AT"][index]
        code, description = values["Wx"]["C"][index]
        if (isinstance(temperature, bool) or isinstance(apparent, bool)
                or not isinstance(temperature, (int, float)) or not isinstance(apparent, (int, float))
                or not math.isfinite(temperature) or not math.isfinite(apparent)):
            raise ValueError("source_format")
        forecast.append({"valid_at": times[index].isoformat(), "temperature_c": temperature,
                         "apparent_temperature_c": apparent, "condition": str(description)[:80],
                         "weather_code": str(code)[:8]})
    return result("ok", {
        "location": region["location"],
        **({"representative_town": region["representative_town"]} if "representative_town" in region else {}),
        "kind": "gridded_forecast", "published_at": published.isoformat(),
        "forecast": forecast, "precipitation_mm": None,
        "source": "中央氣象署",
        "source_url": "https://www.cwa.gov.tw/V8/C/W/Town/Town.html?TID=" + region["town_id"],
    })


async def get_weather(location: str, date: str | None = None, *, client=None) -> dict:
    matches = search_regions(location)
    if not matches:
        return result("unavailable", reason="region_not_found")
    if len(matches) != 1:
        return result("unavailable", {"candidates": [r["location"] for r in matches[:6]],
                                     "candidate_count": len(matches)}, "ambiguous_region")
    now = local_time()
    if date is not None:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError("date_format")
            requested = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return result("error", reason="invalid_date")
        if not now.date() <= requested <= now.date() + timedelta(days=2):
            return result("unavailable", reason="forecast_out_of_range")

    async def fetch(http):
        async with http.stream("GET", "https://www.cwa.gov.tw/Data/js/3hr/ChartData_3hr_T_" + matches[0]["county_id"] + ".js") as response:
            response.raise_for_status()
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > 512 * 1024:
                    raise ValueError("source_size")
            return parse_forecast(payload.decode("utf-8"), matches[0], date, now)

    try:
        async with asyncio.timeout(5):
            if client is not None:
                return await fetch(client)
            async with httpx.AsyncClient(timeout=4, follow_redirects=False) as http:
                return await fetch(http)
    except (httpx.HTTPError, TimeoutError):
        return result("unavailable", reason="weather_source_unavailable")
    except (ValueError, KeyError, TypeError, IndexError, SyntaxError):
        return result("error", reason="weather_source_format")
