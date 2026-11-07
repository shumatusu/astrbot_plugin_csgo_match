#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
独立验证脚本：不依赖 AstrBot，直接请求完美世界电竞真实接口，
打印前 3 场赛事的名称与比分，用于证明逆向结果可用。

用法：
    python tools/probe_api.py                 # 今天
    python tools/probe_api.py 2026-10-31      # 指定日期
    python tools/probe_api.py 2026-10-31 2399718   # 顺便打印某场详情

接口（2026-11 实测）：
    GET https://gwapi.pwesports.cn/eventcenter/app/csgo/event/getMatchList?matchTime=YYYY-MM-DD 00:00:00
    GET https://gwapi.pwesports.cn/eventcenter/app/csgo/event/getMatchDetail?matchId=xxx
    GET https://gwapi.pwesports.cn/eventcenter/app/match/getMatchPlayerScoreListRealtime?matchId=xxx
    无需 token / 签名 / Cookie，仅需带 Referer 访问 cdn 图片时使用。
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

try:
    import httpx
except ImportError:  # 便于在未装 httpx 的环境里用内置桩跑通
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "stubs"))
    import httpx  # type: ignore

BASE = "https://gwapi.pwesports.cn"
PATH_LIST = "/eventcenter/app/csgo/event/getMatchList"
PATH_DETAIL = "/eventcenter/app/csgo/event/getMatchDetail"
PATH_REALTIME = "/eventcenter/app/match/getMatchPlayerScoreListRealtime"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
    "Referer": "https://news.wmpvp.com/",
    "Accept": "application/json, text/plain, */*",
}

STATUS = {1: "进行中", 2: "未开始", 3: "已结束"}


def get_json(path, params):
    url = BASE + path
    with httpx.Client(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        resp = client.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(f"业务错误 code={payload.get('code')} {payload.get('message')}")
    return payload.get("result") or {}, url


def main():
    day = date.today().isoformat()
    want_detail = ""
    args = sys.argv[1:]
    if args and args[0][:2] == "20":
        day = args[0]
        args = args[1:]
    if args:
        want_detail = args[0]

    print(f"== 请求赛程 {day} ==")
    result, url = get_json(PATH_LIST, {"matchTime": f"{day} 00:00:00"})
    rows = ((result.get("matchResponse") or {}).get("dtoList")) or []
    print(f"URL: {url}?matchTime={day} 00:00:00")
    print(f"返回 {len(rows)} 场\n")

    for i, m in enumerate(rows[:3], 1):
        t1 = (m.get("team1DTO") or {}).get("name", "?")
        t2 = (m.get("team2DTO") or {}).get("name", "?")
        ev = (m.get("csgoEventDTO") or {}).get("name", "")
        ts = datetime.fromtimestamp(m["startTime"] / 1000).strftime("%m-%d %H:%M")
        st = STATUS.get(m.get("status"), m.get("status"))
        print(f"[{i}] {t1} {m.get('score1')} : {m.get('score2')} {t2}")
        print(f"    {ev} ｜ {st} ｜ BO{(m.get('bo') or '').replace('bo', '')} ｜ {ts}")
        print(f"    matchId={m.get('matchId')}")
    print()

    target = want_detail or next((str(m["matchId"]) for m in rows if m.get("status") == 3), "")
    if not target and rows:
        target = str(rows[0]["matchId"])
    if not target:
        print("没有可展示的比赛")
        return 0

    print(f"== 请求详情 matchId={target} ==")
    result, url = get_json(PATH_DETAIL, {"matchId": target})
    match = result.get("match") or {}
    t1 = (match.get("team1DTO") or {}).get("name", "?")
    t2 = (match.get("team2DTO") or {}).get("name", "?")
    print(f"URL: {url}?matchId={target}")
    print(f"{t1} {match.get('score1')} : {match.get('score2')} {t2}  ({STATUS.get(match.get('status'))})")

    stats = match.get("statsDTOList") or []
    for seg in stats:
        name = ((seg.get("mapDTO") or {}).get("nameEn")) or ""
        if name.lower() == "all":
            continue
        print(f"  图 · {name:<10} {seg.get('score1')} : {seg.get('score2')}")

    overview = next((s for s in stats if ((s.get("mapDTO") or {}).get("nameEn") == "all")), None)
    if overview:
        for key, label in (("team1PlayerStatsDTOList", t1), ("team2PlayerStatsDTOList", t2)):
            players = overview.get(key) or []
            print(f"  {label}:")
            for p in players:
                nm = (p.get("playerDTO") or {}).get("name", "?")
                print(f"    {nm:<12} KD {p.get('kills')}-{p.get('deaths')}  "
                      f"ADR {p.get('adr')}  RT {p.get('rating')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
