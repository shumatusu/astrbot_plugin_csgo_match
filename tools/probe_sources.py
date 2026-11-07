#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立验证两个新数据源，各自打印前 3 场赛事与比分。

用法：
    python tools/probe_sources.py            # 两个源都测
    python tools/probe_sources.py 5e         # 只测 5EPlay
    python tools/probe_sources.py wanmei     # 只测完美世界数据中心

不依赖 AstrBot / 插件运行时，只用 httpx（或 tests/stubs 里的同步桩）。
"""

import hashlib
import hmac
import json
import secrets
import sys
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "stubs"))
    import httpx  # type: ignore

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")

# ---- 5EPlay ---------------------------------------------------------------- #
FIVEPLAY_BASE = "https://esports-data.5eplaycdn.com"
FIVEPLAY_LIST = "/v1/api/csgo/matches"
FIVEPLAY_DETAIL = "/v1/api/csgo/matches/{mid}/data"
FIVEPLAY_STATUS = {"0": "未开始", "1": "进行中", "2": "已结束", "-1": "未开始"}

# ---- 完美世界数据中心 -------------------------------------------------------- #
WM_BASE = "https://esports.wanmei.com"
WM_PREFIX = "/apg/eventcenter/csgo"
WM_KEY = "828a59393861babc4a27e23f87fc77ab8dc9347dc6550c5e7e51e9b1af2de812"
WM_STATUS = {1: "进行中", 2: "未开始", 3: "已结束"}


def _client():
    return httpx.Client(timeout=15, headers={"User-Agent": UA}, follow_redirects=True)


# =========================================================================== #
def probe_5eplay():
    print("=" * 62)
    print("== 5EPlay 赛事中心（无需鉴权）")
    print("=" * 62)
    url = FIVEPLAY_BASE + FIVEPLAY_LIST
    with _client() as c:
        r = c.get(url, params={"page": 1, "limit": 100})
        print(f"GET {url}?page=1&limit=100")
        print(f"HTTP {r.status_code}  {len(r.content)} bytes")
        payload = r.json()
        if not payload.get("success"):
            print("失败:", payload.get("message"))
            return False
        data = payload.get("data") or {}
        rows = (data.get("matches") or []) + (data.get("live_matches") or [])
        print(f"共 {len(rows)} 场\n")

        for m in rows[:3]:
            mc, st, tt = m.get("mc_info") or {}, m.get("state") or {}, m.get("tt_info") or {}
            t1 = (mc.get("t1_info") or {}).get("disp_name", "?")
            t2 = (mc.get("t2_info") or {}).get("disp_name", "?")
            ts = datetime.fromtimestamp(int(mc.get("plan_ts") or 0)).strftime("%m-%d %H:%M")
            print(f"  {t1} {st.get('t1_score')} : {st.get('t2_score')} {t2}")
            print(f"    {tt.get('disp_name')} ｜ {FIVEPLAY_STATUS.get(str(st.get('status')))} ｜ "
                  f"BO{mc.get('format')} ｜ {ts}")
            print(f"    matchId={mc.get('id')}")

        # 详情：选手数据
        done = [m for m in rows if str((m.get("state") or {}).get("status")) == "2"]
        if done:
            mid = (done[0].get("mc_info") or {}).get("id")
            det_url = FIVEPLAY_BASE + FIVEPLAY_DETAIL.format(mid=mid)
            d = c.get(det_url).json()
            match = ((d.get("data") or {}).get("match")) or {}
            gs = match.get("global_state") or {}
            ps = (gs.get("t1_player_stats") or []) + (gs.get("t2_player_stats") or [])
            print(f"\n  详情 {mid} → {len(ps)} 名选手")
            for p in ps[:3]:
                print(f"    {p.get('name'):<12} K-D {p.get('kill')}-{p.get('death')}  "
                      f"ADR {p.get('adr')}  RT {p.get('rating')}")
    return True


# =========================================================================== #
def _wm_enc(v):
    return urllib.parse.quote(str(v), safe="-_.!~*'()").replace("%20", "+")


def _wm_headers(params):
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    groups = {k: [str(v)] for k, v in (params or {}).items()}
    groups["timestamp"] = [ts]
    groups["nonce"] = [nonce]
    payload = "&".join(f"{_wm_enc(k)}={_wm_enc(v)}"
                       for k in sorted(groups) for v in sorted(groups[k]))
    sig = hmac.new(WM_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {"Referer": "https://data.wanmei.com/",
            "Accept": "application/json, text/plain, */*",
            "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig}


def probe_wanmei():
    print("=" * 62)
    print("== 完美世界数据中心（HMAC-SHA256 动态签名）")
    print("=" * 62)
    today = date.today().isoformat()
    params = {"matchTime": f"{today} 00:00:00"}
    url = WM_BASE + WM_PREFIX + "/getMatchList"
    with _client() as c:
        r = c.get(url, params=params, headers=_wm_headers(params))
        print(f"GET {url}?matchTime={today} 00:00:00")
        print(f"HTTP {r.status_code}  {len(r.content)} bytes")
        payload = r.json()
        if payload.get("code") not in (0, "0"):
            print("失败: code=", payload.get("code"), payload.get("message"))
            return False
        rows = ((payload.get("result") or {}).get("matchResponse") or {}).get("dtoList") or []
        print(f"共 {len(rows)} 场\n")

        for m in rows[:3]:
            t1 = (m.get("team1DTO") or {}).get("name", "?")
            t2 = (m.get("team2DTO") or {}).get("name", "?")
            ev = (m.get("csgoEventDTO") or {}).get("name", "")
            ts = datetime.fromtimestamp(int(m.get("startTime") or 0) / 1000).strftime("%m-%d %H:%M")
            print(f"  {t1} {m.get('score1')} : {m.get('score2')} {t2}")
            print(f"    {ev} ｜ {WM_STATUS.get(m.get('status'))} ｜ {ts}")
            print(f"    matchId={m.get('matchId')}")

        done = [m for m in rows if m.get("status") == 3]
        if done:
            mid = str(done[0].get("matchId"))
            p2 = {"matchId": mid}
            url2 = WM_BASE + WM_PREFIX + "/getMatchDetail"
            d = c.get(url2, params=p2, headers=_wm_headers(p2)).json()
            match = ((d.get("result") or {}).get("match")) or {}
            segs = match.get("statsDTOList") or []
            maps = [((s.get("mapDTO") or {}).get("nameEn"), s.get("score1"), s.get("score2"))
                    for s in segs if ((s.get("mapDTO") or {}).get("nameEn", "")).lower() != "all"]
            ov = next((s for s in segs
                       if ((s.get("mapDTO") or {}).get("nameEn", "")).lower() == "all"), None)
            n = 0
            if ov:
                n = len(ov.get("team1PlayerStatsDTOList") or []) + \
                    len(ov.get("team2PlayerStatsDTOList") or [])
            print(f"\n  详情 {mid} → {len(maps)} 张图, {n} 名选手")
            print("    地图:", maps)
    return True


def main():
    which = (sys.argv[1].lower() if len(sys.argv) > 1 else "all")
    ok = True
    if which in ("all", "5e", "5eplay"):
        ok &= probe_5eplay()
        print()
    if which in ("all", "wanmei", "wanmei_data", "wm"):
        ok &= probe_wanmei()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
