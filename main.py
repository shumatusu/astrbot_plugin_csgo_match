# -*- coding: utf-8 -*-
"""
AstrBot 插件 · 完美世界电竞 CSGO 赛事助手

功能：
  /csgo 赛程                     查询近期赛事列表
  /csgo 详情 <比赛ID>            高保真白底数据卡
  /csgo 比分 <队伍名>            队伍相关比赛
  /csgo 队伍订阅 <队伍名>        订阅队伍（支持缩写/中文别名）
  /csgo 赛事订阅 <赛事名>        订阅赛事（支持带空格全名/模糊匹配）
  /csgo 队伍取消 <队伍名>        取消队伍订阅
  /csgo 赛事取消 <赛事名>        取消赛事订阅
  /csgo 我的订阅                 仅展示当前会话的订阅
  /csgo 测试 渲染|推送|API       开发者调试指令集

设计要点：
  * 订阅全部按 unified_msg_origin 隔离，推送只回到订阅来源会话。
  * 地图级状态机推送：图X开赛 / 图X结束（数据卡） / 整场结束。
  * 三级渲染降级：html_render → Playwright → 纯文本。
  * 自适应轮询：live 60s / 今日有赛 300s / 空闲 600s / 夜间 1800s，失败指数退避。
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import hmac
import secrets
import shutil
import json
import os
import random
import re
import tempfile
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
except Exception:  # pragma: no cover - 运行环境缺少依赖时插件仍需可加载
    httpx = None

try:
    from jinja2 import Template as _JinjaTemplate
except Exception:  # pragma: no cover
    _JinjaTemplate = None

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # 不同 AstrBot 版本导出位置略有差异
    from astrbot.api.event import MessageChain  # type: ignore
except Exception:  # pragma: no cover
    try:
        from astrbot.api.message import MessageChain  # type: ignore
    except Exception:
        MessageChain = None


PLUGIN_NAME = "astrbot_plugin_wmpvp_csgo"
PLUGIN_VERSION = "1.2.0"

# 数据卡宽度。模板 CSS 的 #card 宽度、<meta viewport> 与 t2i 的视口宽度
# 必须三者一致，否则截图会多出右侧白边。
CARD_WIDTH = 720

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
    "Referer": "https://news.wmpvp.com/",
    "Origin": "https://news.wmpvp.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# --------------------------------------------------------------------------- #
# 真实接口（逆向自 https://news.wmpvp.com/csgo-event.html#/matchs，2026-11 实测）
#
#   网关 gwapi.pwesports.cn，GET + 查询参数，无 token、无签名、无 Cookie。
#   统一信封：{"code":0,"message":"success","result":{...}}
#   列表按天拉取：getMatchList?matchTime=YYYY-MM-DD 00:00:00（空格编码为 +）
# --------------------------------------------------------------------------- #
API_BASE = "https://gwapi.pwesports.cn"
PATH_MATCH_LIST = "/eventcenter/app/csgo/event/getMatchList"
PATH_MATCH_DETAIL = "/eventcenter/app/csgo/event/getMatchDetail"
PATH_REALTIME = "/eventcenter/app/match/getMatchPlayerScoreListRealtime"
# VRS 积分：初始分/排名 + 两种胜负分支的加减分（分析页用）
PATH_RANK_PREDICT = "/eventcenter/app/csgo/event/getRankScoreChangePredict"
# 评论（社区接口，token 传空即可，无需登录）
PATH_COMMENT_COUNT = "/appuser/community/comment/getCommentCount"
PATH_COMMENT_LIST = "/appuser/community/comment/getCommentList"
# 赛前预告相关：提前开打的补推窗口 / 改期判定阈值（秒）
EARLY_START_GRACE = 600        # 开赛后 10 分钟内仍补推一次预告
RESCHEDULE_RESET = 1800        # 开赛时间变动超过 30 分钟视为改期
DELAYED_GRACE = 7200           # 被前面比赛压后时，最多延后 2 小时仍补推
BLOCK_SPAN = 6 * 3600          # 只把 6 小时内的前序比赛视为「同一时段」
MAX_BLOCK_WAIT = 1800          # 最多压 30 分钟；上游 live 状态卡住时不能永久不推

COMMENT_ENTITY_TYPE = 8      # 8 = 赛事/比赛实体
COMMENT_PAGE_SIZE = 5
PATH_MATCH_COUNT = "/eventcenter/app/csgo/event/getMatchCountByPeriod"

# 官方 status：1=进行中 2=未开始 3=已结束
API_STATUS_MAP = {1: "live", 2: "upcoming", 3: "finished"}

# 官方 bo 字段是字符串 "bo3"/"bo5"
API_BO_RE = re.compile(r"(\d+)")

# mapId → 地图英文名（实测确认的取值；未收录的回退到中文名映射）
MAP_ID_TO_EN = {
    1: "Cache", 2: "Dust2", 3: "Mirage", 4: "Inferno", 5: "Nuke",
    14: "Ancient", 15: "Anubis", 16: "Train", 17: "Overpass", 18: "Vertigo",
}

# 中文地图名 → 英文名（realtime 接口只给中文名）
MAP_ZH_TO_EN = {
    "核子危机": "Nuke", "炼狱小镇": "Inferno", "荒漠迷城": "Mirage",
    "炙热沙城2": "Dust2", "炙热沙城Ⅱ": "Dust2", "炙热沙城": "Dust2",
    "远古遗迹": "Ancient", "阿努比斯": "Anubis", "死城之谜": "Cache",
    "列车停放站": "Train", "死亡游乐园": "Overpass",
    "殒命大厦": "Vertigo", "运河水城": "Canals", "总览": "All",
}

# 地图背景图兜底表。
# 首选是 Counter-Strike Wiki 的 CS2 加载图（1920x1080 原图，这里取 640px 缩略图），
# 其次用详情接口 mapBPDTOS[].mapUrl（动态、覆盖新图），最后才是这张静态表。
MAP_IMAGE_WIKI = {
    "ancient": "https://static.wikia.nocookie.net/cswikia/images/5/5c/De_ancient_cs2.png/revision/latest/scale-to-width-down/640?cb=20250815011913",
    "anubis": "https://static.wikia.nocookie.net/cswikia/images/d/da/De_anubis_cs2.png/revision/latest/scale-to-width-down/640?cb=20260122020942",
    "cache": "https://static.wikia.nocookie.net/cswikia/images/5/5b/De_cache_cs2.png/revision/latest/scale-to-width-down/640?cb=20260429100503",
    "dust2": "https://static.wikia.nocookie.net/cswikia/images/1/16/Cs2_dust2.png/revision/latest/scale-to-width-down/640?cb=20230913150804",
    "inferno": "https://static.wikia.nocookie.net/cswikia/images/1/17/Cs2_inferno_remake.png/revision/latest/scale-to-width-down/640?cb=20260304235624",
    "mirage": "https://static.wikia.nocookie.net/cswikia/images/f/f5/De_mirage_cs2.png/revision/latest/scale-to-width-down/640?cb=20230807124319",
    "nuke": "https://static.wikia.nocookie.net/cswikia/images/d/d6/De_nuke_cs2.png/revision/latest/scale-to-width-down/640?cb=20240426010253",
    "overpass": "https://static.wikia.nocookie.net/cswikia/images/5/55/Overpass_loading_screen.png/revision/latest/scale-to-width-down/640?cb=20250730205333",
    "train": "https://static.wikia.nocookie.net/cswikia/images/2/2c/De_train_cs2_new.png/revision/latest/scale-to-width-down/640?cb=20250730205931",
    "vertigo": "https://static.wikia.nocookie.net/cswikia/images/8/88/De_vertigo_cs2.jpg/revision/latest/scale-to-width-down/640?cb=20231009185617",
}

MAP_IMAGE_FALLBACK = {
    "ancient": "https://cdn.wmpvp.com/appadmin/1B68B72C0F3241DF889342F725EFDFA9-1793379617263.jpeg",
    "anubis": "https://cdn.wmpvp.com/appadmin/16F8049D49D24CCCA8D6A9DA5743127E-1793379617478.jpeg",
    "cache": "https://cdn.wmpvp.com/appadmin/C5EF8C37618D4DEF9B45B911F90E38B7-1793379615024.jpeg",
    "dust2": "https://cdn.wmpvp.com/appadmin/122BE06414C148C9A722613189D66766-1793379615153.jpeg",
    "inferno": "https://cdn.wmpvp.com/appadmin/F2E0DF349B0C469AAC46C329461B32C4-1793379615461.jpeg",
    "mirage": "https://cdn.wmpvp.com/appadmin/8CC64673988140AB9DDA8EBCFA047956-1793379615272.jpeg",
    "nuke": "https://cdn.wmpvp.com/appadmin/138C104553214F2EAF7C1966E1A70188-1793379615594.jpeg",
    # 当前图池之外的老图，取自 csgo.com.cn 官方资料站
    "train": "https://www.csgo.com.cn/hd/1610/info/images/demo/map_list02.jpg",
    "overpass": "https://www.csgo.com.cn/hd/1610/info/images/demo/map_list06.jpg",
}

# 各图床对 Referer 的要求不同，缺了直接 403
IMAGE_REFERERS = {
    "static.wikia.nocookie.net": "https://counterstrike.fandom.com/",
    "cdn.wmpvp.com": "https://news.wmpvp.com/",
    "oss.5eplay.com": "https://event.5eplay.com/",
}

# --------------------------------------------------------------------------- #
# 多数据源（2026-11 实测逆向）
#
#   gw  = gwapi.pwesports.cn          完美电竞网关，无鉴权（插件首选/原有线路）
#   wm  = esports.wanmei.com/apg/...  完美世界数据中心，HMAC-SHA256 签名，与 gw 同源同数据
#   5e  = esports-data.5eplaycdn.com  5EPlay 赛事中心，无鉴权，字段结构完全不同
# --------------------------------------------------------------------------- #
SOURCE_GW = "gw"
SOURCE_WM = "wm"
SOURCE_5E = "5e"

SOURCE_LABELS = {
    SOURCE_5E: "5EPlay 赛事中心",
    SOURCE_WM: "完美世界数据中心",
    SOURCE_GW: "完美电竞网关",
}

# 配置里允许写可读名或内部短名
SOURCE_ALIASES = {
    "5eplay": SOURCE_5E, "5e": SOURCE_5E, "esports-data": SOURCE_5E,
    "wanmei_data": SOURCE_WM, "wanmei": SOURCE_WM, "wm": SOURCE_WM, "data_wanmei": SOURCE_WM,
    "wmpvp": SOURCE_GW, "gwapi": SOURCE_GW, "gw": SOURCE_GW,
}

DEFAULT_SOURCE_PRIORITY = [SOURCE_5E, SOURCE_WM, SOURCE_GW]

# -- 完美世界数据中心 ------------------------------------------------------- #
WANMEI_DATA_BASE = "https://esports.wanmei.com"
WANMEI_DATA_PREFIX = "/apg/eventcenter/csgo"
WANMEI_DATA_REFERER = "https://data.wanmei.com/"
# 签名密钥，来自 data.wanmei.com 的 JS bundle（csgo-*.js 内 Np 常量）
WANMEI_SIGN_KEY = "828a59393861babc4a27e23f87fc77ab8dc9347dc6550c5e7e51e9b1af2de812"

# -- 5EPlay ---------------------------------------------------------------- #
FIVEPLAY_BASE = "https://esports-data.5eplaycdn.com"
FIVEPLAY_LIST_PATH = "/v1/api/csgo/matches"
FIVEPLAY_DETAIL_TPL = "/v1/api/csgo/matches/{match_id}/data"
FIVEPLAY_REFERER = "https://event.5eplay.com/"
# state.status / bout_states[].status：0=未开始 1=进行中 2=已结束；detail 里未打的图是 -1
FIVEPLAY_STATUS_MAP = {"0": "upcoming", "1": "live", "2": "finished", "-1": "upcoming"}

STATUS_UPCOMING = "upcoming"
STATUS_LIVE = "live"
STATUS_FINISHED = "finished"

STATUS_TEXT = {
    STATUS_UPCOMING: "未开始",
    STATUS_LIVE: "进行中",
    STATUS_FINISHED: "已结束",
}

# 常见队伍别名 → 规范名（用于 /csgo 队伍订阅 Faze / 小蜜蜂 这类输入）
TEAM_ALIASES: Dict[str, str] = {
    "faze": "FaZe Clan", "fazeclan": "FaZe Clan", "银河战舰": "FaZe Clan",
    "vitality": "Vitality", "teamvitality": "Vitality", "小蜜蜂": "Vitality",
    "navi": "Natus Vincere", "natusvincere": "Natus Vincere", "天生赢家": "Natus Vincere",
    "g2": "G2 Esports", "g2esports": "G2 Esports",
    "spirit": "Team Spirit", "teamspirit": "Team Spirit", "绿龙": "Team Spirit",
    "mouz": "MOUZ", "mousesports": "MOUZ", "老鼠": "MOUZ",
    "astralis": "Astralis", "a队": "Astralis",
    "heroic": "Heroic", "liquid": "Team Liquid", "tl": "Team Liquid",
    "vp": "Virtus.pro", "virtuspro": "Virtus.pro",
    "c9": "Cloud9", "cloud9": "Cloud9",
    "furia": "FURIA", "黑豹": "FURIA",
    "pain": "paiN Gaming", "paimgaming": "paiN Gaming",
    "imperial": "Imperial",
    "eternalfire": "Eternal Fire", "ef": "Eternal Fire",
    "mongolz": "The MongolZ", "themonogolz": "The MongolZ", "蒙古": "The MongolZ",
    "lvg": "Lynn Vision", "lynnvision": "Lynn Vision",
    "tyloo": "TYLOO", "天禄": "TYLOO",
    "ra": "Rare Atom", "rareampon": "Rare Atom", "rareatom": "Rare Atom",
    "wingsup": "Wings Up", "wingsupgaming": "Wings Up",
    "newhappy": "Newhappy", "nh": "Newhappy",
    "steelhelmet": "Steel Helmet", "钢盔": "Steel Helmet",
    "catevil": "CatEvil",
    "sharks": "Sharks", "9z": "9z Team",
    "big": "BIG", "complexity": "Complexity", "col": "Complexity",
    "nrg": "NRG", "falcons": "Team Falcons", "猎鹰": "Team Falcons",
    "aurora": "Aurora", "3dmax": "3DMAX", "saw": "SAW", "apeks": "Apeks",
    "fnatic": "fnatic", "nip": "Ninjas in Pyjamas", "og": "OG", "ence": "ENCE",
    "gamerlegion": "GamerLegion", "gl": "GamerLegion",
    "wildcard": "Wildcard", "mibr": "MIBR", "legacy": "Legacy",
    "nemiga": "Nemiga", "b8": "B8", "passionua": "Passion UA",
}

EVENT_HINTS: Dict[str, str] = {
    "major": "Major",
    "iem": "IEM",
    "blast": "BLAST",
    "pgl": "PGL",
    "esl": "ESL",
    "cct": "CCT",
    "完美世界": "完美世界",
    "卡托维兹": "IEM Katowice",
    "科隆": "IEM Cologne",
}


# --------------------------------------------------------------------------- #
# Mock 数据（/csgo 测试 渲染 使用；demo_mode 开启时也用它跑通全链路）
# --------------------------------------------------------------------------- #

MOCK_MATCH_DATA: Dict[str, Any] = {
    # 与 gwapi.pwesports.cn 的原始响应结构保持一致，
    # 这样 demo_mode 与 /csgo 测试 渲染 走的是同一套解析逻辑。
    "id": 900001,
    "matchId": 900001,
    "eventId": 1001,
    "startTime": 1793433600000,
    "team1Id": 1,
    "team1DTO": {"teamId": 1, "name": "FaZe Clan", "logoBlack": "", "rank": 3},
    "team2Id": 2,
    "team2DTO": {"teamId": 2, "name": "Vitality", "logoBlack": "", "rank": 1},
    "score1": 2,
    "score2": 1,
    "bo": "bo3",
    "status": 3,
    "stageName": "Group A",
    "csgoEventDTO": {"eventId": 1001, "name": "IEM 成都 2026", "nameZh": "IEM成都2026"},
    "statsDTOList": [
        {
            "mapDTO": {"nameEn": "all", "nameZh": "总览"},
            "score1": 2, "score2": 1,
            "team1PlayerStatsDTOList": [
                {"playerDTO": {"teamId": 1, "name": "broky", "image": ""},
                 "kills": 67, "deaths": 54, "adr": 70.8, "rating": 1.18, "swing": "+2.31%"},
                {"playerDTO": {"teamId": 1, "name": "rain", "image": ""},
                 "kills": 58, "deaths": 59, "adr": 78.2, "rating": 1.02, "swing": "+0.42%"},
                {"playerDTO": {"teamId": 1, "name": "karrigan", "image": ""},
                 "kills": 41, "deaths": 62, "adr": 55.4, "rating": 0.86, "swing": "-1.15%"},
                {"playerDTO": {"teamId": 1, "name": "EliGE", "image": ""},
                 "kills": 63, "deaths": 57, "adr": 82.1, "rating": 1.11, "swing": "+1.88%"},
                {"playerDTO": {"teamId": 1, "name": "frozen", "image": ""},
                 "kills": 71, "deaths": 55, "adr": 88.6, "rating": 1.24, "swing": "+3.02%"},
            ],
            "team2PlayerStatsDTOList": [
                {"playerDTO": {"teamId": 2, "name": "ZywOo", "image": ""},
                 "kills": 82, "deaths": 57, "adr": 99.6, "rating": 1.56, "swing": "+5.34%"},
                {"playerDTO": {"teamId": 2, "name": "apEX", "image": ""},
                 "kills": 48, "deaths": 66, "adr": 61.3, "rating": 0.91, "swing": "-0.88%"},
                {"playerDTO": {"teamId": 2, "name": "flameZ", "image": ""},
                 "kills": 69, "deaths": 61, "adr": 84.7, "rating": 1.09, "swing": "+1.42%"},
                {"playerDTO": {"teamId": 2, "name": "mezii", "image": ""},
                 "kills": 55, "deaths": 63, "adr": 72.5, "rating": 0.97, "swing": "-0.31%"},
                {"playerDTO": {"teamId": 2, "name": "ropz", "image": ""},
                 "kills": 74, "deaths": 58, "adr": 86.9, "rating": 1.21, "swing": "+2.76%"},
            ],
        },
        {"mapDTO": {"nameEn": "mirage", "nameZh": "荒漠迷城"}, "score1": 13, "score2": 8},
        {"mapDTO": {"nameEn": "inferno", "nameZh": "炼狱小镇"}, "score1": 10, "score2": 13},
        {"mapDTO": {"nameEn": "nuke", "nameZh": "核子危机"}, "score1": 13, "score2": 11},
    ],
}


# --------------------------------------------------------------------------- #
# 高保真白底数据卡模板（Jinja2 语法，AstrBot html_render 与 Playwright 共用）
# --------------------------------------------------------------------------- #

MATCH_DETAIL_TMPL_STR = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<!-- 告诉 t2i 服务用 720 宽视口截图，避免出现右侧留白（服务会解析这个 meta） -->
<meta name="viewport" content="width=720">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  /* 隐藏滚动条：否则会占掉 15px 布局宽度，导致右侧出现白边或内容被挤 */
  ::-webkit-scrollbar{width:0;height:0}
  html{scrollbar-width:none}
  html,body{background:#ffffff}
  body{font-family:"PingFang SC","Microsoft YaHei","Segoe UI",Roboto,sans-serif;
       color:#12161c;-webkit-font-smoothing:antialiased}
  #card{width:720px;margin:0 auto;background:#ffffff;border:1px solid #e8eaee;
        border-radius:20px;padding:24px 26px 16px}
  .top{display:flex;align-items:center;justify-content:space-between;gap:14px}
  .event{font-size:17px;font-weight:700;letter-spacing:.2px;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap;max-width:520px}
  .badge{font-size:12px;font-weight:600;padding:4px 11px;border-radius:999px;white-space:nowrap}
  .badge.live{background:#fdeceb;color:#c62828}
  .badge.upcoming{background:#edf2ff;color:#2f5fd0}
  .badge.finished{background:#f1f3f5;color:#5f6b7a}
  .meta{margin-top:7px;font-size:12px;color:#8a94a3}
  .score-row{display:flex;align-items:center;justify-content:space-between;margin:20px 0 4px}
  .team{width:200px;text-align:center}
  .logo{width:76px;height:76px;border-radius:18px;background:#f5f6f8;margin:0 auto 10px;
        position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center}
  .logo img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
  .logo .ph{display:none;font-size:26px;font-weight:800;color:#c2c8d2}
  .logo.noimg .ph{display:block}
  .tname{font-size:15px;font-weight:700;line-height:1.3}
  .score{flex:1;text-align:center}
  .nums{font-size:44px;font-weight:800;line-height:1;letter-spacing:1px}
  .nums .win{color:#12161c}
  .nums .lose{color:#9aa4b2}
  .colon{margin:0 12px;color:#c8ced8;font-weight:600}
  .sub{font-size:12px;color:#8a94a3;margin-top:8px}
  .section{margin-top:18px}
  .section-title{font-size:11px;font-weight:700;color:#8a94a3;letter-spacing:1.4px;
                 margin-bottom:8px}

  /* 战队支持率 */
  .sup-wrap{background:#fafbfc;border-radius:10px;padding:10px 12px}
  .sup-nums{display:flex;align-items:baseline;justify-content:space-between;
            font-size:12px;margin-bottom:7px}
  .sup-a{font-weight:800;font-size:15px;color:#c8102e}
  .sup-b{font-weight:800;font-size:15px;color:#1f6fd0}
  .sup-t{color:#8a94a3;font-size:11px;text-align:center;flex:1;padding:0 8px;
         white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .sup-bar{display:flex;height:8px;border-radius:5px;overflow:hidden;background:#e8eaee}
  .sup-seg{height:100%}
  .sup-seg-a{background:linear-gradient(90deg,#e03e52,#c8102e)}
  .sup-seg-b{background:linear-gradient(90deg,#4a8fe0,#1f6fd0)}

  /* 赛前预告：倒计时 + 热评 */
  .countdown{display:inline-flex;align-items:center;gap:6px;margin-top:8px;
             padding:4px 10px;border-radius:999px;background:#fff4e6;color:#c26a00;
             font-size:11px;font-weight:700}
  .cmt{background:#fafbfc;border-radius:10px;padding:9px 12px;margin-bottom:8px}
  .cmt:last-child{margin-bottom:0}
  .cmt-head{display:flex;align-items:center;justify-content:space-between;
            font-size:11px;color:#8a94a3;margin-bottom:4px}
  .cmt-user{font-weight:700;color:#3d4653}
  .cmt-like{color:#e8792a;font-weight:700}
  .cmt-body{font-size:12px;color:#2b323c;line-height:1.5;
            display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
            overflow:hidden}

  /* VRS 积分 */
  .vrs{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}
  .vrs th{font-weight:600;color:#a4adba;font-size:10px;text-align:right;padding:0 6px 6px 0;
          border-bottom:1px solid #eef1f5}
  .vrs th.name{text-align:left;padding-left:2px}
  .vrs td{padding:7px 6px 7px 0;text-align:right;border-bottom:1px solid #f4f6f8}
  .vrs td.name{text-align:left;padding-left:2px;width:42%}
  .vrs tr:last-child td{border-bottom:none}
  .vrs .pname{display:flex;align-items:center;gap:7px;font-weight:600}
  .vrs .mono{font-variant-numeric:tabular-nums;font-weight:700}
  .vrs .dim{color:#9aa4b2;font-weight:600}
  .vrs .up{color:#c8102e}
  .vrs .down{color:#1a8b4a}
  .vrs .flat{color:#b3bcc8}
  .vrs-dot{width:18px;height:18px;border-radius:50%;flex:0 0 auto;display:flex;
           align-items:center;justify-content:center;font-size:9px;font-weight:800;
           color:#fff}
  .dot-a{background:#c8102e}
  .dot-b{background:#1f6fd0}
  .map-row{display:flex;align-items:center;justify-content:space-between;padding:0 12px 0 14px;
           height:48px;border-radius:10px;background:#fafbfc;margin-bottom:8px;font-size:13px;
           background-size:cover;background-position:center center;background-repeat:no-repeat;
           position:relative;overflow:hidden}
  .map-scrim{position:absolute;inset:0;
             background:linear-gradient(90deg,rgba(8,10,14,.80) 0%,rgba(8,10,14,.44) 46%,rgba(8,10,14,.66) 100%)}
  .map-left{display:flex;align-items:center;gap:8px;font-weight:600;position:relative;z-index:1}
  .map-row.has-bg .map-left{color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.5)}
  .map-row.has-bg .dot{box-shadow:0 0 0 2px rgba(255,255,255,.55)}
  .dot{width:6px;height:6px;border-radius:50%;background:#cfd6df}
  .dot.live{background:#e53935}
  .dot.completed{background:#9aa4b2}
  .map-score{font-weight:700;font-variant-numeric:tabular-nums;position:relative;z-index:1}
  .map-row.has-bg .map-score{background:rgba(255,255,255,.94);border-radius:7px;
                             padding:5px 10px;line-height:1}
  .map-score .w{color:#12161c}
  .map-score .l{color:#9aa4b2}
  .two-col{display:flex;gap:22px}
  .col{flex:1;min-width:0}
  .col + .col{border-left:1px solid #f0f2f5;padding-left:18px}
  table.players{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:right;color:#98a2b3;font-weight:600;padding:4px 6px;font-size:11px}
  th.name{text-align:left}
  td{padding:7px 6px;border-top:1px solid #f0f2f5;text-align:right;
     font-variant-numeric:tabular-nums}
  td.name{text-align:left;font-weight:600}
  .pname{display:flex;align-items:center;gap:7px}
  .avatar{width:20px;height:20px;border-radius:50%;background:#eef1f5;position:relative;
          overflow:hidden;flex:0 0 auto;display:flex;align-items:center;justify-content:center;
          font-size:9px;color:#b3bcc8;font-weight:700}
  .avatar .ph{display:none}
  .avatar.noimg .ph{display:block}
  .avatar img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .r-high{color:#c62828;font-weight:700}
  .r-mid{color:#b26a00;font-weight:700}
  .r-low{color:#5f6b7a;font-weight:700}
  .footer{margin-top:16px;padding-top:10px;border-top:1px solid #f0f2f5;
          display:flex;justify-content:space-between;font-size:11px;color:#a3acb9}
  .empty{font-size:12px;color:#a3acb9;padding:6px 2px}
  .hint{margin-top:16px;padding:12px 14px;background:#fafbfc;border-radius:10px;
        font-size:12px;color:#8a94a3;text-align:center}
</style>
</head>
<body>
<div id="card">
  <div class="top">
    <div class="event">{{ event_name }}</div>
    <div class="badge {{ status_class }}">{{ status_text }}</div>
  </div>
  <div class="meta">{{ meta_line }}</div>
  {% if countdown %}<div class="countdown">⏰ {{ countdown }}</div>{% endif %}

  <div class="score-row">
    <div class="team">
      <div class="logo{% if not team_a_logo %} noimg{% endif %}">
        {% if team_a_logo %}<img src="{{ team_a_logo }}" alt="" onerror="this.remove();this.parentNode.classList.add('noimg')">{% endif %}
        <span class="ph">{{ team_a_initial }}</span>
      </div>
      <div class="tname">{{ team_a }}</div>
    </div>
    <div class="score">
      <div class="nums">
        <span class="{{ score_a_class }}">{{ score_a }}</span><span class="colon">:</span><span class="{{ score_b_class }}">{{ score_b }}</span>
      </div>
      <div class="sub">{{ score_hint }}</div>
    </div>
    <div class="team">
      <div class="logo{% if not team_b_logo %} noimg{% endif %}">
        {% if team_b_logo %}<img src="{{ team_b_logo }}" alt="" onerror="this.remove();this.parentNode.classList.add('noimg')">{% endif %}
        <span class="ph">{{ team_b_initial }}</span>
      </div>
      <div class="tname">{{ team_b }}</div>
    </div>
  </div>

  {% if not maps and not a_players and not b_players %}
  <div class="hint">{{ empty_hint }}</div>
  {% endif %}

  {% if support %}
  <div class="section">
    <div class="section-title">战队支持率{% if support.total %} · {{ support.total }} 人参与{% endif %}</div>
    <div class="sup-wrap">
      <div class="sup-nums">
        <span class="sup-a">{{ support.a }}%</span>
        <span class="sup-t">{{ team_a }} ｜ {{ team_b }}</span>
        <span class="sup-b">{{ support.b }}%</span>
      </div>
      <div class="sup-bar">
        <div class="sup-seg sup-seg-a" style="width:{{ support.a }}%"></div>
        <div class="sup-seg sup-seg-b" style="width:{{ support.b }}%"></div>
      </div>
    </div>
  </div>
  {% endif %}

  {% if vrs_rows %}
  <div class="section">
    <div class="section-title">VRS 积分变动预测</div>
    <table class="vrs">
      <thead>
        <tr><th class="name">战队</th><th>当前 VRS</th><th>排名</th><th>胜</th><th>负</th></tr>
      </thead>
      <tbody>
        {% for v in vrs_rows %}
        <tr>
          <td class="name">
            <div class="pname">
              <div class="vrs-dot {{ 'dot-a' if v.is_a else 'dot-b' }}">{{ v.initial }}</div>
              <span>{{ v.name }}</span>
            </div>
          </td>
          <td class="mono">{{ v.score }}</td>
          <td class="mono dim">{{ v.rank }}</td>
          <td class="mono {{ v.win_cls }}">{{ v.win }}</td>
          <td class="mono {{ v.lose_cls }}">{{ v.lose }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

  {% if maps %}
  <div class="section">
    <div class="section-title">地图比分</div>
    {% for m in maps %}
    <div class="map-row{% if m.img %} has-bg{% endif %}"{% if m.img %} style="background-image:url('{{ m.img }}')"{% endif %}>
      {% if m.img %}<div class="map-scrim"></div>{% endif %}
      <div class="map-left"><span class="dot {{ m.cls }}"></span>{{ m.label }}</div>
      <div class="map-score">
        <span class="{{ 'w' if m.sa > m.sb else 'l' }}">{{ m.sa }}</span>
        <span style="color:#c8ced8">:</span>
        <span class="{{ 'w' if m.sb > m.sa else 'l' }}">{{ m.sb }}</span>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if comments %}
  <div class="section">
    <div class="section-title">热评</div>
    {% for c in comments %}
    <div class="cmt">
      <div class="cmt-head">
        <span class="cmt-user">{{ c.user }}{% if c.region %} · {{ c.region }}{% endif %}</span>
        <span class="cmt-like">👍 {{ c.likes }}</span>
      </div>
      <div class="cmt-body">{{ c.content }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if a_players or b_players %}
  <div class="section">
    <div class="section-title">选手数据</div>
    <div class="two-col">
      {% for group in [a_players, b_players] %}
      <div class="col">
        {% if group %}
        <table class="players">
          <thead>
            <tr>
              <th class="name">选手</th><th>K-D</th><th>±</th><th>ADR</th><th>RT</th>
            </tr>
          </thead>
          <tbody>
            {% for p in group %}
            <tr>
              <td class="name">
                <div class="pname">
                  <div class="avatar{% if not p.avatar %} noimg{% endif %}">
                    {% if p.avatar %}<img src="{{ p.avatar }}" alt="" onerror="this.remove();this.parentNode.classList.add('noimg')">{% endif %}
                    <span class="ph">{{ p.initial }}</span>
                  </div>
                  <span>{{ p.nickname }}</span>
                </div>
              </td>
              <td>{{ p.kd }}</td>
              <td>{{ p.swing }}</td>
              <td>{{ p.adr }}</td>
              <td class="{{ p.rating_cls }}">{{ p.rating }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}
        <div class="empty">暂无选手数据</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="footer">
    <span>{{ footer_note }}</span>
    <span>{{ generated_at }}</span>
  </div>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

def _acronym(text: Any) -> str:
    """英文名首字母缩写，用来兜住「IEM」↔「Intel Extreme Masters」这类跨源命名差异。"""
    if text is None:
        return ""
    parts = re.split(r"[^0-9A-Za-z]+", str(text))
    return "".join(p[0].lower() for p in parts if p and p[0].isalpha())


def _norm(text: Any) -> str:
    """归一化：小写 + 去掉空白与常见分隔符，用于别名比对。"""
    if text is None:
        return ""
    s = str(text).strip().lower()
    return re.sub(r"[\s\-_.·、,，/|]+", "", s)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).strip())
    except Exception:
        return default


def _pick(raw: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """按候选键取值（大小写不敏感，支持嵌套一层）。"""
    if not isinstance(raw, dict):
        return default
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for k in keys:
        v = lowered.get(k.lower())
        if v not in (None, ""):
            return v
    return default


def _to_ts(value: Any) -> int:
    """时间统一成秒级时间戳；兼容毫秒、秒、字符串时间。"""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10_000_000_000 else v
    s = str(value).strip()
    if s.isdigit():
        return _to_ts(int(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s[:len(fmt) + 2].strip(), fmt).timestamp())
        except Exception:
            continue
    return 0


def _fmt_ts(ts: int, with_time: bool = True) -> str:
    if not ts:
        return "时间待定"
    try:
        dt = datetime.fromtimestamp(ts)
    except Exception:
        return "时间待定"
    now = datetime.now()
    if dt.date() == now.date():
        prefix = "今天"
    elif dt.date() == (now + timedelta(days=1)).date():
        prefix = "明天"
    elif dt.date() == (now - timedelta(days=1)).date():
        prefix = "昨天"
    else:
        prefix = f"{dt.month}月{dt.day}日"
    return f"{prefix} {dt.strftime('%H:%M')}" if with_time else prefix


def _norm_status(value: Any) -> str:
    """把各家五花八门的状态字段映射成 upcoming / live / finished。"""
    if value is None:
        return STATUS_UPCOMING
    s = str(value).strip().lower()
    if s in ("2", "live", "ongoing", "playing", "running", "in_progress", "进行中", "开赛中"):
        return STATUS_LIVE
    if s in ("3", "4", "finished", "finish", "over", "ended", "complete", "completed",
             "done", "已结束", "结束", "完赛"):
        return STATUS_FINISHED
    if s in ("0", "1", "upcoming", "notstarted", "not_started", "pending", "scheduled",
             "waiting", "未开始", "未开赛", "待开始"):
        return STATUS_UPCOMING
    if "live" in s or "进行" in s:
        return STATUS_LIVE
    if "finish" in s or "end" in s or "结束" in s:
        return STATUS_FINISHED
    return STATUS_UPCOMING


def _initial(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "?"
    return name[0].upper()


def _disp_width(text: Any) -> int:
    """终端/聊天窗口里的显示宽度：CJK 字符按 2 列计。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text or ""))


def _pad(text: Any, width: int) -> str:
    """按显示宽度右侧补空格，保证纯文本卡片列对齐。"""
    s = str(text or "")
    return s + " " * max(width - _disp_width(s), 1)


def _rating_cls(rating: float) -> str:
    if rating >= 1.15:
        return "r-high"
    if rating >= 1.0:
        return "r-mid"
    return "r-low"


def _drop_unplayed_maps(match: Dict[str, Any]) -> None:
    """已结束的比赛去掉没打的图，并同步重排逐图选手索引。

    5EPlay 的详情会把未打的决胜图也列出来（0:0），直接展示很误导。
    注意要在「列表比分合并进详情」之后调用，否则全是 0:0 判断不出来。
    """
    maps = match.get("maps") or []
    if match.get("status") != STATUS_FINISHED or not maps:
        return
    played = [(i, m) for i, m in enumerate(maps)
              if _safe_int(m.get("score_a")) or _safe_int(m.get("score_b"))]
    if not played or len(played) == len(maps):
        return

    keep_old = [i for i, _ in played]
    match["maps"] = [dict(m, map_index=new) for new, (_, m) in enumerate(played)]
    per_map = match.get("map_players") or {}
    if per_map:
        match["map_players"] = {new: per_map[old]
                                for new, old in enumerate(keep_old) if old in per_map}


def _reorder_map_players(match: Dict[str, Any]) -> None:
    """逐图选手按整场阵容顺序排列。

    接口返回的逐图数据是按 Rating 降序的，不重排的话同一名选手在图1/图2
    会跳到不同行，看起来像换了人。
    """
    order = {p.get("nickname"): i for i, p in enumerate(match.get("players") or [])}
    if not order:
        return
    for rows in (match.get("map_players") or {}).values():
        rows.sort(key=lambda x: order.get(x.get("nickname"), 999))


def _named(m: Dict[str, Any]) -> bool:
    """两边队名都是真实名字（不是 TBD / 空），否则卡片没意义。"""
    a = str(m.get("team_a") or "").strip()
    b = str(m.get("team_b") or "").strip()
    if not a or not b:
        return False
    return "TBD" not in (a + b).upper()


def _signed(value: Any) -> str:
    """带符号的数字：34 → "+34"，-12 → "-12"，0/None → "0"。"""
    n = _safe_int(value)
    return f"{n:+d}" if n else "0"


# --------------------------------------------------------------------------- #
# 接口异常
# --------------------------------------------------------------------------- #

class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body or ""

    def brief(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.body:
            parts.append(self.body[:120].replace("\n", " "))
        return " ｜ ".join(parts)


# --------------------------------------------------------------------------- #
# 完美世界电竞 API 客户端
# --------------------------------------------------------------------------- #

class WmpvpClient:
    """完美世界电竞赛事网关客户端。

    逆向结论（2026-11 实测）：
      * 网关 gwapi.pwesports.cn 无鉴权，GET + 查询参数即可，不需要 token/签名/Cookie；
      * 列表按天返回，单日 30~40 场，字段 csgoEventDTO.name / team1DTO / score1 / status；
      * 详情 getMatchDetail 带 statsDTOList（总览 + 逐图比分 + 5v5 选手数据）；
      * 实时 getMatchPlayerScoreListRealtime 给出当前打到第几张图（boxNum）与实时选手数据。
    """

    def __init__(self, base: str = API_BASE,
                 list_path: str = PATH_MATCH_LIST,
                 detail_path: str = PATH_MATCH_DETAIL,
                 realtime_path: str = PATH_REALTIME,
                 timeout: float = 10.0,
                 demo_mode: bool = False,
                 schedule_days: int = 1):
        self.source_id = SOURCE_GW
        self.base = (base or API_BASE).rstrip("/")
        self.list_path = list_path or PATH_MATCH_LIST
        self.detail_path = detail_path or PATH_MATCH_DETAIL
        self.realtime_path = realtime_path or PATH_REALTIME
        self.timeout = timeout
        self.demo_mode = demo_mode
        self.schedule_days = max(1, int(schedule_days or 1))

        self._client: Optional["httpx.AsyncClient"] = None
        self._list_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._detail_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._list_ttl = 20.0
        self._detail_ttl = 60.0
        self.last_error: Optional[ApiError] = None
        self.last_probe: List[Dict[str, Any]] = []

    # -- 底层 ------------------------------------------------------------- #

    async def _get_client(self) -> "httpx.AsyncClient":
        if httpx is None:
            raise ApiError("缺少 httpx 依赖，请先 pip install httpx")
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(6.0, self.timeout)),
                headers=HEADERS,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None

    async def raw_get(self, url: str, params: Optional[Dict[str, Any]] = None,
                      headers: Optional[Dict[str, str]] = None
                      ) -> Tuple[int, str, Optional[Any]]:
        client = await self._get_client()
        resp = await client.get(url, params=params, headers=headers)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = None
        return resp.status_code, text, payload

    def sign_headers(self, params: Dict[str, Any]) -> Dict[str, str]:
        """子类可覆盖：为每个请求生成鉴权头（默认无）。"""
        return {}

    async def fetch_binary(self, url: str) -> Tuple[int, bytes, str]:
        """抓二进制资源（队徽/头像/地图背景）。

        图床普遍校验 Referer，缺失直接 403，所以按域名补对应来源：
        cdn.wmpvp.com 要 news.wmpvp.com，static.wikia.nocookie.net 要 fandom。
        """
        client = await self._get_client()
        referer = ""
        for host, ref in IMAGE_REFERERS.items():
            if host in url:
                referer = ref
                break
        headers = {"Referer": referer} if referer else None
        resp = await client.get(url, headers=headers)
        return (resp.status_code,
                getattr(resp, "content", b"") or b"",
                (resp.headers.get("content-type") or "") if hasattr(resp, "headers") else "")

    async def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """请求并拆信封：{"code":0,"message":"success","result":{...}}"""
        url = self.base + path
        try:
            status, text, payload = await self.raw_get(url, params, self.sign_headers(params))
        except Exception as exc:
            raise ApiError(f"请求异常：{exc}") from exc

        if status != 200:
            raise ApiError(f"HTTP 异常（{url}）", status=status, body=text)
        if not isinstance(payload, dict):
            raise ApiError(f"响应不是 JSON（{url}）", status=status, body=text)

        code = payload.get("code")
        if code not in (0, "0", None):
            msg = payload.get("message") or "未知错误"
            raise ApiError(f"接口返回 code={code} {msg}", status=status, body=text)

        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    # -- 线路探测（/csgo 测试 API） --------------------------------------- #

    async def probe(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        captured: Dict[str, Any] = {}
        today = date.today().isoformat()

        async def _record(label: str, path: str, params: Dict[str, Any],
                          counter, ok_fn=None) -> Optional[Dict[str, Any]]:
            url = self.base + path
            item = {"label": label, "url": url, "params": params, "status": None,
                    "elapsed_ms": 0, "body": "", "count": 0, "note": "", "ok": False}
            started = time.time()
            try:
                status, text, payload = await self.raw_get(
                    url, params, self.sign_headers(params or {}))
                item["status"] = status
                item["body"] = (text or "")[:400]
                if status == 200 and isinstance(payload, dict):
                    code = payload.get("code")
                    if code in (0, "0", None):
                        result = payload.get("result") or {}
                        captured[label] = result
                        item["count"], item["note"] = counter(result)
                        # 各接口的"成功"标准不同：详情/实时即使暂时没有数据，
                        # 只要返回了结构正确的对象也算连通正常。
                        item["ok"] = ok_fn(result) if ok_fn else item["count"] > 0
                    else:
                        item["note"] = f"code={code} {payload.get('message')}"
                else:
                    item["note"] = f"HTTP {status}"
            except Exception as exc:
                item["body"] = f"EXC: {exc}"
                item["note"] = "请求异常"
            item["elapsed_ms"] = int((time.time() - started) * 1000)
            results.append(item)
            return item

        def _list_note(r: Dict[str, Any]):
            n = len(((r.get("matchResponse") or {}).get("dtoList") or []))
            return n, f"{n} 场比赛"

        await _record(
            "赛事列表", self.list_path, {"matchTime": f"{today} 00:00:00"}, _list_note,
        )

        rows = ((captured.get("赛事列表") or {}).get("matchResponse") or {}).get("dtoList") or []
        match_id = ""
        live_id = ""
        if rows:
            # 详情优先挑已结束的比赛（有逐图比分），否则取第一场
            finished = [r for r in rows if _safe_int(r.get("status")) == 3]
            match_id = str((finished or rows)[0].get("matchId"))
            for r in rows:
                if _safe_int(r.get("status")) == 1:
                    live_id = str(r.get("matchId"))
                    break

        if match_id:
            def _detail_note(r: Dict[str, Any]):
                segs = len(((r.get("match") or {}).get("statsDTOList") or []))
                return segs, (f"{segs} 个分段" if segs else "暂无分段（比赛未开始或未产生数据）")

            await _record(
                "比赛详情", self.detail_path, {"matchId": match_id}, _detail_note,
                ok_fn=lambda r: bool((r.get("match") or {}).get("matchId")),
            )

        if live_id:
            def _realtime_note(r: Dict[str, Any]):
                players = len((r.get("team1PlayerList") or []))
                box = _safe_int(r.get("boxNum"))
                name = str(r.get("mapName") or "")
                extra = f"，图{box} {name}" if box else ""
                return players, f"{players} 名选手{extra}"

            if getattr(self, "supports_realtime", True):
                await _record(
                    "实时比分", self.realtime_path, {"matchId": live_id}, _realtime_note,
                    ok_fn=lambda r: bool(r.get("mapName") or r.get("team1PlayerList")),
                )

        self.last_probe = results
        return results

    # -- 列表 ------------------------------------------------------------- #

    async def fetch_match_list(self, day: Optional[date] = None,
                               force: bool = False) -> List[Dict[str, Any]]:
        """拉取某一天（默认今天）的赛事列表。"""
        if self.demo_mode:
            return [self.normalize_match(MOCK_MATCH_DATA)]

        target = day or date.today()
        key = target.isoformat()
        now = time.time()
        cached = self._list_cache.get(key)
        if not force and cached and (now - cached[0]) < self._list_ttl:
            return cached[1]

        result = await self._get_json(self.list_path, {"matchTime": f"{key} 00:00:00"})
        rows = ((result.get("matchResponse") or {}).get("dtoList")) or []
        if not isinstance(rows, list):
            rows = []

        matches: List[Dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            # 与前端一致：completedStatus 1~4 为取消/延期等异常态，直接跳过
            if raw.get("completedStatus") in (1, 2, 3, 4):
                continue
            item = self.normalize_match(raw)
            if item.get("match_id") and (item.get("team_a") or item.get("team_b")):
                matches.append(item)

        matches.sort(key=lambda m: (m.get("start_time") or 0))
        self._list_cache[key] = (time.time(), matches)
        self.last_error = None
        return matches

    async def fetch_window(self, days: Optional[int] = None,
                           force: bool = False) -> List[Dict[str, Any]]:
        """今天起连续 N 天的赛事（按 match_id 去重）。"""
        span = max(1, int(days if days is not None else self.schedule_days))
        today = date.today()
        merged: Dict[str, Dict[str, Any]] = {}
        last_exc: Optional[ApiError] = None
        for offset in range(span):
            try:
                rows = await self.fetch_match_list(today + timedelta(days=offset), force=force)
            except ApiError as exc:
                last_exc = exc
                continue
            for m in rows:
                merged.setdefault(str(m.get("match_id")), m)
        if not merged and last_exc is not None:
            raise last_exc
        return sorted(merged.values(), key=lambda m: (m.get("start_time") or 0))

    # -- 详情 / 实时 ------------------------------------------------------ #

    async def fetch_match_detail(self, match_id: str,
                                 force: bool = False) -> Optional[Dict[str, Any]]:
        if self.demo_mode:
            if str(match_id) == str(MOCK_MATCH_DATA.get("matchId")):
                return self.normalize_match(MOCK_MATCH_DATA)
            return None

        key = str(match_id)
        now = time.time()
        cached = self._detail_cache.get(key)
        if not force and cached and (now - cached[0]) < self._detail_ttl:
            return cached[1]

        result = await self._get_json(self.detail_path, {"matchId": key})
        raw = result.get("match")
        if not isinstance(raw, dict):
            return None

        match = self.normalize_match(raw)
        self._apply_detail(match, raw)
        self._detail_cache[key] = (time.time(), match)
        return match

    async def fetch_realtime(self, match_id: str) -> Optional[Dict[str, Any]]:
        if self.demo_mode:
            return None
        try:
            result = await self._get_json(self.realtime_path, {"matchId": str(match_id)})
        except ApiError:
            return None
        if not result or not result.get("mapName"):
            return None
        return result

    # -- 扩展数据：VRS / 评论 ---------------------------------------------- #

    async def fetch_rank_predict(self, match_id: str) -> Optional[Dict[str, Any]]:
        """VRS 积分：初始分与排名 + 两种胜负结果的加减分。

        返回结构（已归一化）：
            {"a": {"name", "score", "rank", "win_delta", "lose_delta"},
             "b": {"name", "score", "rank", "win_delta", "lose_delta"}}
        胜者加、败者减；若接口只给赢家一侧的增量，另一侧取相反数。
        """
        if self.demo_mode:
            return None
        try:
            r = await self._get_json(PATH_RANK_PREDICT, {"matchId": str(match_id)})
        except ApiError:
            return None
        if not isinstance(r, dict) or not r:
            return None

        init = r.get("initialRankings") or {}
        t1 = init.get("team1") or {}
        t2 = init.get("team2") or {}
        if not (t1 or t2):
            return None

        # 四个分支的真实增量，不靠正负对称去猜
        w1 = r.get("ifTeam1Wins") or {}
        w2 = r.get("ifTeam2Wins") or {}
        a_win = _safe_int(((w1.get("team1") or {}).get("scoreChange")))
        a_lose = _safe_int(((w2.get("team1") or {}).get("scoreChange")))
        b_win = _safe_int(((w2.get("team2") or {}).get("scoreChange")))
        b_lose = _safe_int(((w1.get("team2") or {}).get("scoreChange")))

        # 比赛结束后该预测恒为 0/0，没有展示价值
        if not (a_win or a_lose or b_win or b_lose):
            return None

        return {
            "a": {"name": str(t1.get("name") or ""),
                  "score": _safe_int(t1.get("score")),
                  "rank": _safe_int(t1.get("globalRank")),
                  "win_delta": a_win, "lose_delta": a_lose},
            "b": {"name": str(t2.get("name") or ""),
                  "score": _safe_int(t2.get("score")),
                  "rank": _safe_int(t2.get("globalRank")),
                  "win_delta": b_win, "lose_delta": b_lose},
        }

    async def fetch_comment_count(self, match_id: str) -> int:
        if self.demo_mode:
            return 0
        try:
            r = await self._get_json(PATH_COMMENT_COUNT, {
                "entityId": str(match_id),
                "entityType": COMMENT_ENTITY_TYPE,
                "token": "",
            })
        except ApiError:
            return 0
        return _safe_int((r or {}).get("count"))

    async def fetch_comments(self, match_id: str, limit: int = COMMENT_PAGE_SIZE,
                             sort: int = 4) -> List[Dict[str, Any]]:
        """热门评论。sort=4 为官方默认（热度）。

        每条归一化为：昵称 / 内容 / 点赞数 / 时间 / 地区 / 回复数 / 是否带图。
        """
        if self.demo_mode:
            return []
        try:
            r = await self._get_json(PATH_COMMENT_LIST, {
                "commentId": 0,
                "entityId": str(match_id),
                "entityType": COMMENT_ENTITY_TYPE,
                "pageNum": 1,
                "pageSize": max(1, int(limit)),
                "sort": sort,
                "token": "",
                "type": 1,
                "onlyOwner": "false",
            })
        except ApiError:
            return []
        block = (r or {}).get("commentResponse") or {}
        rows = block.get("commentDTOS") or []
        out: List[Dict[str, Any]] = []
        for c in rows:
            if not isinstance(c, dict):
                continue
            content = str(c.get("content") or "").replace("\n", " ").strip()
            if not content:
                continue
            out.append({
                "user": str(c.get("userName") or "匿名"),
                "content": content,
                "likes": _safe_int(c.get("likeCount")),
                "time": str(c.get("createTimeText") or ""),
                "region": str(c.get("userRegion") or ""),
                "replies": _safe_int(c.get("replyCount")),
                "has_img": bool(c.get("hasImg")),
            })
        out.sort(key=lambda x: -x["likes"])
        return out

    @staticmethod
    def _parse_support_rate(raw: Dict[str, Any], team_a: Dict[str, Any],
                            team_b: Dict[str, Any]) -> Dict[str, Any]:
        """解析 teamSupportRateMap → {"a": 67, "b": 33}（百分比整数）。

        该字段键是队伍 ID，需要按当前对阵的两队 ID 取；缺任一侧就不展示，
        避免把别的比赛的支持率错配到本场。
        """
        rate_map = raw.get("teamSupportRateMap")
        if not isinstance(rate_map, dict) or not rate_map:
            return {}
        id_a = str(team_a.get("teamId") or raw.get("team1Id") or "")
        id_b = str(team_b.get("teamId") or raw.get("team2Id") or "")
        a = rate_map.get(id_a)
        b = rate_map.get(id_b)
        if a is None or b is None:
            return {}
        a, b = _safe_int(a), _safe_int(b)
        total = a + b
        if total <= 0:
            return {}
        # 保证两边加起来正好 100
        a_pct = round(a * 100 / total)
        return {"a": a_pct, "b": 100 - a_pct,
                "votes_a": a, "votes_b": b, "votes_total": total}

    # -- 归一化 ----------------------------------------------------------- #

    def normalize_match(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}

        team_a = raw.get("team1DTO") or {}
        team_b = raw.get("team2DTO") or {}
        event = raw.get("csgoEventDTO") or {}

        bo_raw = str(raw.get("bo") or "")
        bo_match = API_BO_RE.search(bo_raw)
        bo = int(bo_match.group(1)) if bo_match else 0

        status = API_STATUS_MAP.get(_safe_int(raw.get("status")), STATUS_UPCOMING)
        start_ts = _to_ts(raw.get("startTime"))

        match = {
            "match_id": str(raw.get("matchId") or raw.get("id") or ""),
            "event_name": str(event.get("name") or ""),
            "event_name_zh": str(event.get("nameZh") or ""),
            "stage": str(raw.get("stageName") or ""),
            "team_a": str(team_a.get("name") or "TBD"),
            "team_b": str(team_b.get("name") or "TBD"),
            "team_a_id": team_a.get("teamId") or raw.get("team1Id") or 0,
            "team_b_id": team_b.get("teamId") or raw.get("team2Id") or 0,
            "team_a_logo": str(team_a.get("logoBlack") or team_a.get("logoWhite") or ""),
            "team_b_logo": str(team_b.get("logoBlack") or team_b.get("logoWhite") or ""),
            "score_a": _safe_int(raw.get("score1")),
            "score_b": _safe_int(raw.get("score2")),
            "status": status,
            "start_time": start_ts,
            "bo": bo,
            "maps": [],
            "map_images": {},
            "map_slots": {},
            "players": [],
            "map_players": {},
            "source": self.source_id,
            # 战队支持率（百分比），键为队伍 ID，来自 teamSupportRateMap
            "support_rate": self._parse_support_rate(raw, team_a, team_b),
            "comment_count": 0,
            "rank_predict": {},
            "raw": raw,
        }

        # 详情响应里已经带 statsDTOList 时，直接解析
        if raw.get("statsDTOList"):
            self._apply_detail(match, raw)
        return match

    def _apply_detail(self, match: Dict[str, Any], raw: Dict[str, Any]) -> None:
        """把 getMatchDetail 的 statsDTOList 解析成逐图比分 + 5v5 选手数据。"""
        # 地图背景图：veto 里每个 mapName 都带一张横幅（即使还没有比分也能拿到）
        images: Dict[str, str] = dict(match.get("map_images") or {})
        for bp in raw.get("mapBPDTOS") or []:
            if isinstance(bp, dict) and bp.get("mapName") and bp.get("mapUrl"):
                images[str(bp["mapName"]).lower()] = str(bp["mapUrl"])
        if images:
            match["map_images"] = images

        # 图序还原：singleMatchDataDTOS 的 boxNum 就是第几张图，mapId 能映射回地图名。
        # 这样即使我们没看到某张图开打，也能知道它是哪张（否则只能显示「图1」占位）。
        slots: Dict[int, str] = dict(match.get("map_slots") or {})
        for sm in raw.get("singleMatchDataDTOS") or []:
            if not isinstance(sm, dict):
                continue
            box = _safe_int(sm.get("boxNum"))
            name = MAP_ID_TO_EN.get(_safe_int(sm.get("mapId")), "")
            if box > 0 and name:
                slots[box] = name
        if slots:
            match["map_slots"] = slots

        stats = raw.get("statsDTOList") or []
        if not isinstance(stats, list) or not stats:
            return

        overview = None
        maps: List[Dict[str, Any]] = []
        for idx, seg in enumerate(stats):
            if not isinstance(seg, dict):
                continue
            map_dto = seg.get("mapDTO") or {}
            name_en = str(map_dto.get("nameEn") or "")
            if name_en.lower() == "all":
                overview = seg
                continue
            name = name_en or MAP_ZH_TO_EN.get(str(map_dto.get("nameZh") or ""), "")
            if not name:
                continue
            maps.append({
                "map_index": len(maps),
                "map_name": name.title(),
                "score_a": _safe_int(seg.get("score1")),
                "score_b": _safe_int(seg.get("score2")),
                "status": STATUS_FINISHED,
            })
        if maps:
            match["maps"] = maps

        if overview is None:
            overview = stats[0] if isinstance(stats[0], dict) else None
        if not overview:
            return

        players = self._parse_wmpvp_players(overview, match)
        if players:
            match["players"] = players

        # 逐图选手数据：图结束卡片要用这一张图的战绩，而不是整场汇总
        map_players: Dict[int, List[Dict[str, Any]]] = {}
        mi = 0
        for seg in stats:
            if not isinstance(seg, dict):
                continue
            if str(((seg.get("mapDTO") or {}).get("nameEn") or "")).lower() == "all":
                continue
            rows = self._parse_wmpvp_players(seg, match)
            if rows:
                map_players[mi] = rows
            mi += 1
        if map_players:
            match["map_players"] = map_players
            _reorder_map_players(match)

        # 详情比列表更准，回填
        if raw.get("endTime") and match.get("status") == STATUS_LIVE:
            pass
        if not match.get("start_time"):
            match["start_time"] = _to_ts(raw.get("startTime"))

    @staticmethod
    def _parse_wmpvp_players(seg: Dict[str, Any],
                             match: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把一个 stats 段（总览或某张图）的 5v5 数据解析成内部选手结构。"""
        players: List[Dict[str, Any]] = []
        for key, tid in (("team1PlayerStatsDTOList", match.get("team_a_id")),
                         ("team2PlayerStatsDTOList", match.get("team_b_id"))):
            for p in seg.get(key) or []:
                if not isinstance(p, dict):
                    continue
                pdt = p.get("playerDTO") or {}
                players.append({
                    "team_id": pdt.get("teamId") or tid,
                    "nickname": str(pdt.get("name") or ""),
                    "avatar": str(pdt.get("image") or ""),
                    "kd": f"{_safe_int(p.get('kills'))}-{_safe_int(p.get('deaths'))}",
                    "swing": str(p.get("swing") or "-"),
                    "adr": f"{_safe_float(p.get('adr')):.1f}",
                    "rating": _safe_float(p.get("rating")),
                })
        return players

    def apply_realtime(self, match: Dict[str, Any], rt: Dict[str, Any]) -> None:
        """用实时接口补全"当前打到第几张图"，并推算每张图的状态。"""
        box = _safe_int(rt.get("boxNum"))
        if box <= 0:
            return
        map_finished = bool(rt.get("finished"))
        cur_name = (MAP_ZH_TO_EN.get(str(rt.get("mapName") or ""))
                    or MAP_ID_TO_EN.get(_safe_int(rt.get("mapId")))
                    or str(rt.get("mapName") or ""))

        bo = match.get("bo") or 3
        existing = {m.get("map_index"): m for m in (match.get("maps") or [])}
        maps: List[Dict[str, Any]] = []
        for i in range(max(bo, box)):
            box_i = i + 1
            prev = existing.get(i) or {}
            if box_i < box:
                status = STATUS_FINISHED
            elif box_i == box:
                status = STATUS_FINISHED if map_finished else STATUS_LIVE
            else:
                status = STATUS_UPCOMING
            # 当前这张图必须用实时接口给的真名；否则先前进来的占位名
            # （"图2"）会一直覆盖真名，导致结束卡片显示成「图2 结束 · 图2」
            if box_i == box and cur_name:
                name = cur_name
            else:
                name = prev.get("map_name") or ""
            # 没亲眼看到开打的图（订阅晚了）用 veto 图序还原真名
            if not name or name == f"图{box_i}":
                name = (match.get("map_slots") or {}).get(box_i) or name
            name = name or f"图{box_i}"
            maps.append({
                "map_index": i,
                "map_name": name,
                "score_a": _safe_int(prev.get("score_a")),
                "score_b": _safe_int(prev.get("score_b")),
                "status": status,
            })
        match["maps"] = maps

        players: List[Dict[str, Any]] = []
        for key, tid in (("team1PlayerList", match.get("team_a_id")),
                         ("team2PlayerList", match.get("team_b_id"))):
            for p in rt.get(key) or []:
                if not isinstance(p, dict):
                    continue
                players.append({
                    "team_id": p.get("teamId") or tid,
                    "nickname": str(p.get("name") or ""),
                    "avatar": str(p.get("logo") or ""),
                    "kd": f"{_safe_int(p.get('kill'))}-{_safe_int(p.get('death'))}",
                    "swing": str(p.get("swing") or "-"),
                    "adr": f"{_safe_float(p.get('adr')):.1f}",
                    "rating": _safe_float(p.get("rating")),
                })
        if players:
            match["players"] = players


class WanmeiDataClient(WmpvpClient):
    """完美世界数据中心（data.wanmei.com）的赛事接口。

    逆向结论（2026-11 实测）：
      * 网关 esports.wanmei.com，路径前缀 /apg/eventcenter/csgo/；
      * **动态签名**：每个请求都要带 X-Timestamp / X-Nonce / X-Signature；
      * 签名 = HMAC-SHA256(待签串, WANMEI_SIGN_KEY) 十六进制；
      * 待签串 = 参数按键名排序，同名多值内部再排序，逐项 enc(k)=enc(v) 用 & 连接，
        并额外并入 timestamp 与 nonce 两个键；
      * 数据与 gwapi.pwesports.cn 完全同源（同 matchId 同 stats），
        因此解析逻辑直接复用 WmpvpClient，仅覆盖 base 与鉴权；
      * 该网关不提供实时接口（/apg 下 getMatchPlayerScoreListRealtime 为 404）。
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("base", WANMEI_DATA_BASE)
        kwargs.setdefault("list_path", f"{WANMEI_DATA_PREFIX}/getMatchList")
        kwargs.setdefault("detail_path", f"{WANMEI_DATA_PREFIX}/getMatchDetail")
        super().__init__(**kwargs)
        self.source_id = SOURCE_WM
        self.supports_realtime = False

    @staticmethod
    def _enc(value: Any) -> str:
        """等价 JS：encodeURIComponent 后把 %20 换成 +。"""
        return urllib.parse.quote(str(value), safe="-_.!~*'()").replace("%20", "+")

    def sign_headers(self, params: Dict[str, Any]) -> Dict[str, str]:
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)

        groups: Dict[str, List[str]] = {}
        for k, v in (params or {}).items():
            groups[k] = [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]
        groups["timestamp"] = [ts]
        groups["nonce"] = [nonce]

        payload = "&".join(
            f"{self._enc(k)}={self._enc(val)}"
            for k in sorted(groups)
            for val in sorted(groups[k])
        )
        sig = hmac.new(WANMEI_SIGN_KEY.encode(), payload.encode(),
                       hashlib.sha256).hexdigest()
        return {
            "Referer": WANMEI_DATA_REFERER,
            "Accept": "application/json, text/plain, */*",
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": sig,
        }

    async def fetch_realtime(self, match_id: str) -> Optional[Dict[str, Any]]:
        # 该网关不暴露实时接口，交给上层降级（地图状态退回详情判断）
        return None


class FivePlayClient:
    """5EPlay 赛事中心（event.5eplay.com）的数据客户端。

    逆向结论（2026-11 实测）：
      * 数据域 esports-data.5eplaycdn.com，**无需任何鉴权**（无 token/sign/cookie）；
      * 列表 GET /v1/api/csgo/matches?page=1&limit=N，一次最多给上百场，
        每场自带 state.bout_states（地图名 + 逐图比分 + 图状态），所以列表就能做地图级推送；
      * 详情 GET /v1/api/csgo/matches/{id}/data，含
        match.global_state.t1_player_stats / t2_player_stats（K/D/ADR/Rating/Swing）；
      * 比赛 ID 形如 csgo_mc_2399901，为与完美系 ID 区分，内部统一加 "5e:" 前缀。
    """

    def __init__(self, timeout: float = 10.0, demo_mode: bool = False,
                 schedule_days: int = 1, page_limit: int = 100):
        self.source_id = SOURCE_5E
        self.base = FIVEPLAY_BASE
        self.list_path = FIVEPLAY_LIST_PATH
        self.detail_path = FIVEPLAY_DETAIL_TPL
        self.timeout = timeout
        self.demo_mode = demo_mode
        self.schedule_days = max(1, int(schedule_days or 1))
        self.page_limit = max(10, int(page_limit or 100))

        self._client: Optional["httpx.AsyncClient"] = None
        self._list_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._detail_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._list_ttl = 20.0
        self._detail_ttl = 60.0
        self.last_error: Optional[ApiError] = None
        self.last_probe: List[Dict[str, Any]] = []

    # -- 底层 ------------------------------------------------------------- #

    async def _get_client(self) -> "httpx.AsyncClient":
        if httpx is None:
            raise ApiError("缺少 httpx 依赖，请先 pip install httpx")
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(6.0, self.timeout)),
                headers={**HEADERS, "Referer": FIVEPLAY_REFERER},
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None

    async def raw_get(self, url: str, params: Optional[Dict[str, Any]] = None,
                      headers: Optional[Dict[str, str]] = None
                      ) -> Tuple[int, str, Optional[Any]]:
        client = await self._get_client()
        resp = await client.get(url, params=params, headers=headers)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = None
        return resp.status_code, text, payload

    async def fetch_binary(self, url: str) -> Tuple[int, bytes, str]:
        client = await self._get_client()
        referer = ""
        for host, ref in IMAGE_REFERERS.items():
            if host in url:
                referer = ref
                break
        resp = await client.get(url, headers={"Referer": referer} if referer else None)
        return (resp.status_code,
                getattr(resp, "content", b"") or b"",
                (resp.headers.get("content-type") or "") if hasattr(resp, "headers") else "")

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
        """5EPlay 信封：{"success":true,"errcode":0,"data":{...}}"""
        url = self.base + path
        try:
            status, text, payload = await self.raw_get(url, params)
        except Exception as exc:
            raise ApiError(f"请求异常：{exc}") from exc
        if status != 200:
            raise ApiError(f"HTTP 异常（{url}）", status=status, body=text)
        if not isinstance(payload, dict):
            raise ApiError(f"响应不是 JSON（{url}）", status=status, body=text)
        if not payload.get("success"):
            msg = payload.get("message") or f"errcode={payload.get('errcode')}"
            raise ApiError(f"接口返回失败：{msg}", status=status, body=text)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # -- 列表 ------------------------------------------------------------- #

    async def fetch_match_list(self, day: Optional[date] = None,
                               force: bool = False) -> List[Dict[str, Any]]:
        """5EPlay 的列表不按天过滤，这里取一页再按本地日期筛。"""
        if self.demo_mode:
            return [self.normalize_match(MOCK_MATCH_DATA)]

        target = day or date.today()
        key = target.isoformat()
        now = time.time()
        cached = self._list_cache.get(key)
        if not force and cached and (now - cached[0]) < self._list_ttl:
            return cached[1]

        data = await self._get_json(self.list_path,
                                    {"page": 1, "limit": self.page_limit})
        rows: List[Dict[str, Any]] = []
        for bucket in ("matches", "live_matches"):
            for raw in data.get(bucket) or []:
                if isinstance(raw, dict):
                    rows.append(raw)

        matches: List[Dict[str, Any]] = []
        for raw in rows:
            item = self.normalize_match(raw)
            if not item.get("match_id"):
                continue
            if item.get("start_time"):
                d = datetime.fromtimestamp(item["start_time"]).date()
                # 只保留目标日；进行中的比赛无论哪天开始都要给（可能跨天）
                if d != target and item.get("status") != STATUS_LIVE:
                    continue
            matches.append(item)

        matches.sort(key=lambda m: (m.get("start_time") or 0))
        self._list_cache[key] = (time.time(), matches)
        self.last_error = None
        return matches

    async def fetch_window(self, days: Optional[int] = None,
                           force: bool = False) -> List[Dict[str, Any]]:
        span = max(1, int(days if days is not None else self.schedule_days))
        today = date.today()
        merged: Dict[str, Dict[str, Any]] = {}
        last_exc: Optional[ApiError] = None
        for offset in range(span):
            try:
                rows = await self.fetch_match_list(today + timedelta(days=offset), force=force)
            except ApiError as exc:
                last_exc = exc
                continue
            for m in rows:
                merged.setdefault(str(m.get("match_id")), m)
        if not merged and last_exc is not None:
            raise last_exc
        return sorted(merged.values(), key=lambda m: (m.get("start_time") or 0))

    # -- 详情 / 实时 ------------------------------------------------------ #

    async def fetch_match_detail(self, match_id: str,
                                 force: bool = False) -> Optional[Dict[str, Any]]:
        key = self._strip(match_id)
        if not key:
            return None
        if self.demo_mode:
            return self.normalize_match(MOCK_MATCH_DATA)
        now = time.time()
        cached = self._detail_cache.get(key)
        if not force and cached and (now - cached[0]) < self._detail_ttl:
            return cached[1]

        data = await self._get_json(self.detail_path.format(match_id=key))
        raw = data.get("match")
        if not isinstance(raw, dict):
            return None
        match = self.normalize_match(raw)
        self._merge_list_scores(match)
        _drop_unplayed_maps(match)      # 合并比分后才能判断哪些图真的打过
        self._apply_players(match, raw)
        self._detail_cache[key] = (time.time(), match)
        return match

    def _merge_list_scores(self, match: Dict[str, Any]) -> None:
        """详情的 bouts_state 不带回合比分，用列表缓存里的值补齐。"""
        mid = str(match.get("match_id") or "")
        if not mid:
            return
        base: Optional[Dict[str, Any]] = None
        for _, rows in self._list_cache.values():
            for m in rows:
                if str(m.get("match_id")) == mid:
                    base = m
                    break
            if base:
                break
        if not base:
            return

        if not (match.get("score_a") or match.get("score_b")):
            match["score_a"] = base.get("score_a", 0)
            match["score_b"] = base.get("score_b", 0)

        by_idx = {m.get("map_index"): m for m in base.get("maps") or []}
        for m in match.get("maps") or []:
            src = by_idx.get(m.get("map_index"))
            if src and not (m.get("score_a") or m.get("score_b")):
                m["score_a"] = src.get("score_a", 0)
                m["score_b"] = src.get("score_b", 0)
                if src.get("status"):
                    m["status"] = src["status"]

    async def fetch_realtime(self, match_id: str) -> Optional[Dict[str, Any]]:
        # 列表/详情的 bout_states 已含当前图与其状态，无需单独的实时接口
        return None

    @staticmethod
    def _strip(match_id: str) -> str:
        text = str(match_id or "")
        if text.startswith(f"{SOURCE_5E}:"):
            text = text[len(SOURCE_5E) + 1:]
        return text

    @staticmethod
    def _pub(match_id: str) -> str:
        text = str(match_id or "")
        return text if text.startswith(f"{SOURCE_5E}:") else f"{SOURCE_5E}:{text}"

    # -- 归一化 ----------------------------------------------------------- #

    def normalize_match(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}

        mc = raw.get("mc_info") or {}
        # 列表用 state，详情用 global_state，两者字段同名
        st = raw.get("state") or raw.get("global_state") or {}
        tt = raw.get("tt_info") or {}
        t1 = mc.get("t1_info") or {}
        t2 = mc.get("t2_info") or {}

        fmt = _safe_int(mc.get("format"))
        bo = fmt if 1 <= fmt <= 9 else 3

        match = {
            "match_id": self._pub(str(mc.get("id") or "")),
            "event_name": str(tt.get("disp_name") or tt.get("abbr_zh")
                              or tt.get("abbr_en") or ""),
            "event_name_zh": str(tt.get("abbr_zh") or ""),
            "stage": str(mc.get("tt_stage_desc") or mc.get("tt_stage") or ""),
            "team_a": str(t1.get("disp_name") or "TBD"),
            "team_b": str(t2.get("disp_name") or "TBD"),
            "team_a_id": str(t1.get("id") or ""),
            "team_b_id": str(t2.get("id") or ""),
            "team_a_logo": str(t1.get("logo") or ""),
            "team_b_logo": str(t2.get("logo") or ""),
            "score_a": _safe_int(st.get("t1_score")),
            "score_b": _safe_int(st.get("t2_score")),
            "status": FIVEPLAY_STATUS_MAP.get(str(st.get("status")), STATUS_UPCOMING),
            "start_time": _safe_int(mc.get("plan_ts")) or 0,
            "bo": bo,
            "maps": [],
            "map_images": {},
            "map_slots": {},
            "map_players": {},
            "source": self.source_id,
            "players": [],
            "raw": raw,
        }

        # 列表里就有逐图数据（bout_states），地图级推送无需再等详情
        for b in st.get("bout_states") or []:
            if not isinstance(b, dict):
                continue
            idx = _safe_int(b.get("bout_num")) - 1
            if idx < 0:
                continue
            match["maps"].append({
                "map_index": idx,
                "map_name": str(b.get("map_name") or f"图{idx + 1}"),
                "score_a": _safe_int(b.get("t1_score")),
                "score_b": _safe_int(b.get("t2_score")),
                "status": FIVEPLAY_STATUS_MAP.get(str(b.get("status")), STATUS_UPCOMING),
            })

        # 详情分支：bouts_state 带完整图序（含未打的 -1），用于补全占位名
        for b in raw.get("bouts_state") or []:
            if not isinstance(b, dict):
                continue
            idx = _safe_int(b.get("bout_num")) - 1
            name = str(b.get("map_name") or "")
            if idx < 0 or not name:
                continue
            match["map_slots"][idx + 1] = name
            rows = self._parse_5e_players(b, match)
            if rows:
                match.setdefault("map_players", {})[idx] = rows
            existing = {m["map_index"]: m for m in match["maps"]}
            if idx not in existing:
                match["maps"].append({
                    "map_index": idx,
                    "map_name": name,
                    "score_a": _safe_int(b.get("t1_score")),
                    "score_b": _safe_int(b.get("t2_score")),
                    "status": FIVEPLAY_STATUS_MAP.get(str(b.get("status")), STATUS_UPCOMING),
                })
            elif not existing[idx].get("map_name") or existing[idx]["map_name"].startswith("图"):
                existing[idx]["map_name"] = name

        match["maps"].sort(key=lambda m: m["map_index"])
        if match["maps"]:
            match["maps"] = [
                dict(m, map_index=i) for i, m in enumerate(match["maps"])
            ]
        _drop_unplayed_maps(match)
        return match

    @staticmethod
    def _parse_5e_players(bout: Dict[str, Any],
                          match: Dict[str, Any]) -> List[Dict[str, Any]]:
        """解析 5EPlay 单张图的 t1_pr_stats / t2_pr_stats。"""
        players: List[Dict[str, Any]] = []
        for key, tid in (("t1_pr_stats", match.get("team_a_id")),
                         ("t2_pr_stats", match.get("team_b_id"))):
            for p in bout.get(key) or []:
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name") or "")
                if not name:
                    continue
                kill = _safe_int(p.get("kill"))
                death = _safe_int(p.get("death"))
                players.append({
                    "team_id": tid or 0,
                    "nickname": name,
                    "avatar": str(p.get("half_portrait") or p.get("portrait") or ""),
                    "kd": f"{kill}-{death}",
                    "swing": str(p.get("swing") or "-"),
                    "adr": f"{_safe_float(p.get('adr')):.1f}",
                    "rating": _safe_float(p.get("rating")),
                })
        return players

    def _apply_players(self, match: Dict[str, Any], raw: Dict[str, Any]) -> None:
        gs = raw.get("global_state") or {}
        players: List[Dict[str, Any]] = []
        for key, tid in (("t1_player_stats", match.get("team_a_id")),
                         ("t2_player_stats", match.get("team_b_id"))):
            for p in gs.get(key) or []:
                if not isinstance(p, dict):
                    continue
                kill = _safe_int(p.get("kill"))
                death = _safe_int(p.get("death"))
                players.append({
                    "team_id": tid or 0,
                    "nickname": str(p.get("name") or ""),
                    "avatar": str(p.get("portrait") or p.get("half_portrait") or ""),
                    "kd": f"{kill}-{death}",
                    "swing": str(p.get("swing") or "-"),
                    "adr": f"{_safe_float(p.get('adr')):.1f}",
                    "rating": _safe_float(p.get("rating")),
                })
        if players:
            match["players"] = players
        _reorder_map_players(match)

    def apply_realtime(self, match: Dict[str, Any], rt: Dict[str, Any]) -> None:
        """5EPlay 不需要额外合并，保留接口以兼容调用方。"""
        return

    # -- 探测 ------------------------------------------------------------- #

    async def probe(self) -> List[Dict[str, Any]]:
        today = date.today().isoformat()
        results: List[Dict[str, Any]] = []

        async def _record(label, path, params, note_fn, ok_fn=None):
            url = self.base + path
            item = {"label": label, "url": url, "params": params, "status": None,
                    "elapsed_ms": 0, "body": "", "count": 0, "note": "", "ok": False}
            started = time.time()
            try:
                status, text, payload = await self.raw_get(url, params)
                item["status"] = status
                item["body"] = (text or "")[:400]
                if status == 200 and isinstance(payload, dict):
                    if payload.get("success"):
                        data = payload.get("data") or {}
                        item["count"], item["note"] = note_fn(data)
                        item["ok"] = ok_fn(data) if ok_fn else item["count"] > 0
                    else:
                        item["note"] = f"errcode={payload.get('errcode')} {payload.get('message')}"
                else:
                    item["note"] = f"HTTP {status}"
            except Exception as exc:
                item["body"] = f"EXC: {exc}"
                item["note"] = "请求异常"
            item["elapsed_ms"] = int((time.time() - started) * 1000)
            results.append(item)
            return item

        def _list_note(data):
            n = len(data.get("matches") or []) + len(data.get("live_matches") or [])
            return n, f"{n} 场比赛"

        await _record("赛事列表", self.list_path, {"page": 1, "limit": self.page_limit},
                      _list_note)

        first_id = ""
        try:
            # 用整页数据挑样本：前若干场往往都是未开赛的，拿不到选手统计
            data = await self._get_json(self.list_path,
                                        {"page": 1, "limit": self.page_limit})
            rows = (data.get("matches") or []) + (data.get("live_matches") or [])
            done = [r for r in rows if str(((r.get("state") or {}).get("status"))) == "2"]
            for cand in (done or rows):
                cid = str((cand.get("mc_info") or {}).get("id") or "")
                if not cid:
                    continue
                first_id = first_id or cid
                # 优先挑有选手数据的场次，探测结果更有参考价值
                try:
                    det = await self._get_json(
                        self.detail_path.format(match_id=cid))
                except ApiError:
                    continue
                gs = ((det.get("match") or {}).get("global_state")) or {}
                if (gs.get("t1_player_stats") or gs.get("t2_player_stats")):
                    first_id = cid
                    break
        except Exception:
            pass

        if first_id:
            def _det_note(data):
                gs = ((data.get("match") or {}).get("global_state")) or {}
                n = len(gs.get("t1_player_stats") or []) + len(gs.get("t2_player_stats") or [])
                return n, f"{n} 名选手"

            await _record("比赛详情", self.detail_path.format(match_id=first_id), None,
                          _det_note, ok_fn=lambda d: bool((d.get("match") or {}).get("mc_info")))

        self.last_probe = results
        return results


class MultiSourceClient:
    """按优先级串起多个数据源，任一成功即返回，全部失败才抛错。

    * 比赛 ID 带源前缀（5EPlay 用 "5e:"），详情/实时按前缀路由；
    * 完美系（gw / wm）ID 同源，保持裸数字，避免历史 state 失效；
    * 记住当前可用源，避免每轮都从第一个开始试。
    """

    def __init__(self, sources: List[Any], locked: bool = False):
        self.sources = [s for s in sources if s is not None]
        if not self.sources:
            raise ApiError("没有可用的数据源")
        self.locked = locked          # True：指定了具体源，失败不切换
        self.active = self.sources[0]
        self.last_error: Optional[ApiError] = None
        self.last_probe: List[Dict[str, Any]] = []
        self.last_source_report: List[Dict[str, Any]] = []

    # -- 兼容被插件直接调用的属性 ------------------------------------------ #

    @property
    def base(self) -> str:
        return getattr(self.active, "base", "")

    @property
    def list_path(self) -> str:
        return getattr(self.active, "list_path", "")

    @property
    def detail_path(self) -> str:
        return getattr(self.active, "detail_path", "")

    @property
    def realtime_path(self) -> str:
        return getattr(self.active, "realtime_path", "")

    @property
    def source_id(self) -> str:
        return getattr(self.active, "source_id", "")

    def _by_id(self, source_id: str):
        for s in self.sources:
            if getattr(s, "source_id", "") == source_id:
                return s
        return None

    async def close(self) -> None:
        for s in self.sources:
            try:
                await s.close()
            except Exception:
                pass

    async def fetch_binary(self, url: str) -> Tuple[int, bytes, str]:
        """抓图统一走当前源（任何源的 fetch_binary 都可抓任意 URL）。"""
        for s in self.sources:
            if hasattr(s, "fetch_binary"):
                try:
                    return await s.fetch_binary(url)
                except Exception:
                    continue
        return 0, b"", ""

    # -- 列表 ------------------------------------------------------------- #

    async def fetch_match_list(self, day: Optional[date] = None,
                               force: bool = False) -> List[Dict[str, Any]]:
        order = [self.active] + [s for s in self.sources if s is not self.active]
        errors: List[str] = []

        for src in order:
            sid = getattr(src, "source_id", "?")
            name = SOURCE_LABELS.get(sid, sid)
            try:
                rows = await src.fetch_match_list(day, force=force)
            except ApiError as exc:
                if self.locked:
                    # 指定了具体源就不许静默切换，原样抛出
                    self.last_error = exc
                    raise
                errors.append(f"{name}: {exc.brief()}")
                continue
            if rows:
                self.active = src
                self.last_error = None
                self.last_source_report = [
                    {"source": getattr(s, "source_id", "?"), "ok": s is src,
                     "count": len(rows) if s is src else 0}
                    for s in self.sources
                ]
                return rows
            errors.append(f"{name}: 未返回比赛")

        self.last_error = ApiError("所有数据源均失败：" + "；".join(errors))
        raise self.last_error

    async def fetch_window(self, days: Optional[int] = None,
                           force: bool = False) -> List[Dict[str, Any]]:
        order = [self.active] + [s for s in self.sources if s is not self.active]
        for src in order:
            try:
                rows = await src.fetch_window(days, force=force)
            except ApiError:
                continue
            if rows:
                self.active = src
                return rows
        raise self.last_error or ApiError("所有数据源均失败")

    # -- 详情 / 实时（按 ID 前缀路由） ------------------------------------- #

    def _route(self, match_id: str) -> List[Any]:
        text = str(match_id or "")
        if text.startswith(f"{SOURCE_5E}:"):
            src = self._by_id(SOURCE_5E)
            return [src] if src else []
        # 完美系（gw/wm）ID 同源，优先当前源，其余作为兜底
        wmpvp_sources = [s for s in self.sources
                         if getattr(s, "source_id", "") in (SOURCE_GW, SOURCE_WM)]
        if not wmpvp_sources:
            return []
        head = [self.active] if self.active in wmpvp_sources else []
        return head + [s for s in wmpvp_sources if s not in head]

    async def fetch_match_detail(self, match_id: str,
                                 force: bool = False) -> Optional[Dict[str, Any]]:
        last_exc: Optional[ApiError] = None
        for src in self._route(match_id):
            try:
                detail = await src.fetch_match_detail(match_id, force=force)
            except ApiError as exc:
                last_exc = exc
                continue
            if detail:
                return detail
        if last_exc:
            raise last_exc
        return None

    async def fetch_realtime(self, match_id: str) -> Optional[Dict[str, Any]]:
        for src in self._route(match_id):
            try:
                rt = await src.fetch_realtime(match_id)
            except ApiError:
                continue
            if rt:
                return rt
        return None

    # -- 归一化：按原始结构分派 -------------------------------------------- #

    def normalize_match(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(raw, dict) and "mc_info" in raw:
            src = self._by_id(SOURCE_5E) or self.sources[0]
        else:
            src = self._by_id(SOURCE_GW) or self.sources[0]
        return src.normalize_match(raw)

    def apply_realtime(self, match: Dict[str, Any], rt: Dict[str, Any]) -> None:
        for s in self.sources:
            if hasattr(s, "apply_realtime"):
                s.apply_realtime(match, rt)
                return

    # -- 探测 ------------------------------------------------------------- #

    async def probe(self) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        for s in self.sources:
            sid = getattr(s, "source_id", "?")
            label = SOURCE_LABELS.get(sid, sid)
            try:
                items = await s.probe()
            except Exception as exc:
                merged.append({"label": f"{label} 整体探测", "url": "", "params": None,
                               "status": None, "elapsed_ms": 0, "body": str(exc),
                               "count": 0, "note": "探测异常", "ok": False})
                continue
            for it in items:
                merged.append(dict(it, label=f"{label} · {it['label']}"))
        self.last_probe = merged
        return merged


class JsonStore:
    """极简 JSON 落盘存储（原子写，失败只告警不抛错）。"""

    def __init__(self, path: Path, default: Dict[str, Any]):
        self.path = path
        self.data: Dict[str, Any] = json.loads(json.dumps(default))
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = raw
        except Exception as exc:
            logger.warning(f"[wmpvp_csgo] 读取 {self.path.name} 失败: {exc}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning(f"[wmpvp_csgo] 写入 {self.path.name} 失败: {exc}")


class SubStore(JsonStore):
    """订阅数据：按 unified_msg_origin 隔离。"""

    def __init__(self, path: Path):
        super().__init__(path, {"teams": {}, "events": {}})

    def _bucket(self, kind: str, umo: str) -> List[Dict[str, Any]]:
        root = self.data.setdefault(kind, {})
        return root.setdefault(umo, [])

    def add_team(self, umo: str, name: str) -> Tuple[bool, str]:
        items = self._bucket("teams", umo)
        key = _norm(name)
        for it in items:
            if _norm(it.get("name")) == key or key in [_norm(a) for a in it.get("aliases", [])]:
                return False, it.get("name", name)
        items.append({"name": name, "aliases": [name], "added_at": int(time.time())})
        self.save()
        return True, name

    def remove_team(self, umo: str, name: str) -> bool:
        items = self._bucket("teams", umo)
        key = _norm(name)
        keep = [it for it in items
                if _norm(it.get("name")) != key and key not in [_norm(a) for a in it.get("aliases", [])]]
        if len(keep) == len(items):
            return False
        self.data["teams"][umo] = keep
        self.save()
        return True

    def add_event(self, umo: str, name: str,
                  matched: Optional[List[str]] = None) -> Tuple[bool, str]:
        items = self._bucket("events", umo)
        key = _norm(name)
        for it in items:
            if _norm(it.get("name")) == key:
                return False, it.get("name", name)
        entry: Dict[str, Any] = {"name": name, "added_at": int(time.time())}
        if matched:
            # 记录匹配到的赛事全名，后续按全名也能命中（关键词本身未必是子串）
            entry["matched"] = list(matched)[:20]
        items.append(entry)
        self.save()
        return True, name

    def remove_event(self, umo: str, name: str) -> bool:
        items = self._bucket("events", umo)
        key = _norm(name)

        def _hit(it: Dict[str, Any]) -> bool:
            if _norm(it.get("name")) == key:
                return True
            # 列表里展示的是全名，所以按全名取消也要生效
            return any(_norm(m) == key for m in (it.get("matched") or []))

        keep = [it for it in items if not _hit(it)]
        if len(keep) == len(items):
            return False
        self.data["events"][umo] = keep
        self.save()
        return True

    def get(self, umo: str) -> Dict[str, List[Dict[str, Any]]]:
        return {
            "teams": list(self.data.get("teams", {}).get(umo, [])),
            "events": list(self.data.get("events", {}).get(umo, [])),
        }

    def all(self) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        umos = set(self.data.get("teams", {})) | set(self.data.get("events", {}))
        out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for umo in umos:
            snapshot = self.get(umo)
            if snapshot["teams"] or snapshot["events"]:
                out[umo] = snapshot
        return out

    def count(self, umo: str) -> int:
        s = self.get(umo)
        return len(s["teams"]) + len(s["events"])


class StateStore(JsonStore):
    """推送指纹：{umo: {match_id: {"maps": {idx: fp}, "finished": bool}}}"""

    def __init__(self, path: Path):
        super().__init__(path, {"sessions": {}})

    def session(self, umo: str) -> Dict[str, Any]:
        return self.data.setdefault("sessions", {}).setdefault(umo, {})

    def get_match(self, umo: str, match_id: str) -> Dict[str, Any]:
        return self.session(umo).get(str(match_id), {})

    def set_match(self, umo: str, match_id: str, value: Dict[str, Any]) -> None:
        self.session(umo)[str(match_id)] = value

    def prune(self, keep_days: int = 3, max_items: int = 200) -> None:
        for umo, bucket in list(self.data.get("sessions", {}).items()):
            if len(bucket) <= max_items:
                continue
            items = sorted(bucket.items(),
                           key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
            self.data["sessions"][umo] = dict(items[:max_items])


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #

def _data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # type: ignore
        base = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
    except Exception:
        base = Path(__file__).resolve().parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


@register(PLUGIN_NAME, "shumatusu", "完美世界电竞 CSGO 赛事查询 / 订阅 / 地图级推送", PLUGIN_VERSION)
class WmpvpCsgoPlugin(Star):

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.name = PLUGIN_NAME

        root = Path(__file__).resolve().parent
        self._root = root
        self._assets_dir = root / "assets" / "maps"
        self._data_dir = _data_dir()
        self._tmp_dir = Path(tempfile.gettempdir()) / "wmpvp_csgo"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        # 配置
        self.render_mode = str(self._cfg("render_mode", "auto")).lower()
        self.render_width = int(self._cfg("render_width", CARD_WIDTH))
        self.test_api_cooldown = int(self._cfg("test_api_cooldown", 10))
        self.test_admin_only = bool(self._cfg("test_command_admin_only", False))
        self.max_subs = int(self._cfg("max_subs_per_session", 10))
        self.push_map_start = bool(self._cfg("push_map_start", True))
        self.push_map_end = bool(self._cfg("push_map_end", True))
        self.push_match_end = bool(self._cfg("push_match_end", True))
        self.push_preview = bool(self._cfg("push_preview", True))
        self.preview_minutes = max(1, _safe_int(self._cfg("preview_minutes", 15), 15))

        # 客户端：按配置的优先级组装多数据源，任一可用即工作
        self.default_source = self._parse_default_source()
        self.source_priority = self._parse_priority()
        self.client = self._build_client()

        # 存储
        self.subs = SubStore(self._data_dir / "subs.json")
        self.state = StateStore(self._data_dir / "state.json")
        # 群内数据源覆盖（/csgo 数据源），优先级高于 WebUI 全局配置
        self.source_override = JsonStore(self._data_dir / "source_override.json", {"groups": {}})

        # 运行时
        self._poll_task: Optional[asyncio.Task] = None
        self._test_api_last: Dict[str, float] = {}
        self._owned_temps: set = set()
        self._img_cache: Dict[str, Tuple[float, str]] = {}
        self._event_cache: Dict[str, Tuple[float, List[str]]] = {}
        self._single_clients: Dict[str, MultiSourceClient] = {}
        self._diag: Dict[str, Any] = {
            "polls": 0, "last_poll": 0, "last_matches": 0, "last_hits": 0,
            "last_error": "", "pushes": 0, "push_images": 0, "push_text_only": 0,
            "last_push": 0, "last_push_note": "", "render_last": "尚未渲染",
        }
        self._tmpl = self._load_template()

    def _parse_default_source(self) -> str:
        """WebUI 下拉选择的默认数据源，非 auto 时禁止静默回退。"""
        raw = str(self._cfg("data_source", "auto") or "auto").strip().lower()
        if raw == "auto":
            return "auto"
        if raw in SOURCE_ALIASES:
            return SOURCE_ALIASES[raw]
        logger.warning(f"[wmpvp_csgo] 未知 data_source={raw!r}，回退 auto")
        return "auto"

    def _parse_priority(self) -> List[str]:
        raw = self._cfg("data_source_priority", "")
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw]
        else:
            items = [x.strip() for x in str(raw or "").split(",")]
        known = [SOURCE_ALIASES[x.lower()] for x in items if x and x.lower() in SOURCE_ALIASES]
        # 去重后补上缺省源，保证「全失败还有兜底」
        ordered: List[str] = []
        for x in known + DEFAULT_SOURCE_PRIORITY:
            if x not in ordered:
                ordered.append(x)
        return ordered

    def _make_source(self, sid: str) -> Any:
        timeout = float(self._cfg("request_timeout", 10))
        demo = bool(self._cfg("demo_mode", False))
        days = int(self._cfg("schedule_days", 1))
        makers = {
            SOURCE_5E: lambda: FivePlayClient(
                timeout=timeout, demo_mode=demo, schedule_days=days),
            SOURCE_WM: lambda: WanmeiDataClient(
                timeout=timeout, demo_mode=demo, schedule_days=days),
            SOURCE_GW: lambda: WmpvpClient(
                base=str(self._cfg("api_base", API_BASE)),
                list_path=str(self._cfg("match_list_path", PATH_MATCH_LIST)),
                detail_path=str(self._cfg("detail_path", PATH_MATCH_DETAIL)),
                realtime_path=str(self._cfg("realtime_path", PATH_REALTIME)),
                timeout=timeout, demo_mode=demo, schedule_days=days),
        }
        if sid not in makers:
            raise ApiError(f"未知数据源：{sid}")
        return makers[sid]()

    def _build_client(self) -> MultiSourceClient:
        demo = bool(self._cfg("demo_mode", False))
        if demo:
            # 演示模式的 MOCK 是完美系结构，固定用 gw 解析，避免被 5EPlay 误读
            sources = [self._make_source(SOURCE_GW)]
        elif self.default_source != "auto":
            # WebUI 指定了具体源：只挂这一个，失败直接报错，不静默切换
            sources = [self._make_source(self.default_source)]
        else:
            sources = [self._make_source(sid) for sid in self.source_priority]
        client = MultiSourceClient(sources, locked=(self.default_source != "auto" and not demo))
        if self.default_source != "auto":
            logger.info(
                f"[wmpvp_csgo] 数据源已锁定为 {SOURCE_LABELS.get(self.default_source)}（不做回退）")
        else:
            logger.info(
                "[wmpvp_csgo] 数据源优先级：" +
                " → ".join(SOURCE_LABELS.get(s.source_id, s.source_id) for s in client.sources)
            )
        return client

        logger.info(f"[wmpvp_csgo] 插件已加载，数据目录: {self._data_dir}")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        self._ensure_poller()

    def _ensure_poller(self) -> None:
        """幂等启动轮询：initialize 未触发时，首条指令也能拉起后台任务。"""
        if not self._cfg("poll_enable", True):
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        try:
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("[wmpvp_csgo] 推送轮询已启动")
        except RuntimeError:
            pass

    async def terminate(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        await self.client.close()
        self.subs.save()
        self.state.save()
        logger.info("[wmpvp_csgo] 插件已卸载")

    # ------------------------------------------------------------------ #
    # 配置 / 模板
    # ------------------------------------------------------------------ #

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    def _load_template(self) -> str:
        path = self._root / "templates" / "match_detail.html"
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except Exception as exc:
            logger.warning(f"[wmpvp_csgo] 读取自定义模板失败，使用内置模板: {exc}")
        return MATCH_DETAIL_TMPL_STR

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #

    @filter.command("csgo")
    async def csgo(self, event: AstrMessageEvent):
        self._ensure_poller()
        raw = (getattr(event, "message_str", "") or "").strip()
        tokens = raw.split()

        # 兼容 "/csgo xxx" 与唤醒前缀被剥离后的 "xxx"
        if tokens and tokens[0].lstrip("/").lower() in ("csgo", "csgo赛事", "cs2"):
            tokens = tokens[1:]

        if not tokens:
            yield event.plain_result(self._help_text())
            return

        sub_cmd = tokens[0]
        param = " ".join(tokens[1:]).strip()
        umo = getattr(event, "unified_msg_origin", "") or ""

        try:
            if sub_cmd in ("帮助", "help", "?", "-h"):
                yield event.plain_result(self._help_text())
                return

            if sub_cmd == "测试":
                async for r in self._handle_test(event, umo, param):
                    yield r
                return

            if sub_cmd in ("赛程", "赛事", "列表"):
                yield event.plain_result(await self._cmd_schedule(param))
                return

            if sub_cmd == "详情":
                if not param:
                    yield event.plain_result("用法：/csgo 详情 <比赛ID>（比赛ID 可用 /csgo 赛程 查看）")
                    return
                async for r in self._cmd_detail(event, param):
                    yield r
                return

            if sub_cmd in ("诊断", "状态", "diag"):
                yield event.plain_result(await self._cmd_diag(param))
                return

            if sub_cmd in ("数据源", "源"):
                yield event.plain_result(await self._cmd_source(umo, param))
                return

            if sub_cmd in ("评论", "热评"):
                if not param:
                    yield event.plain_result("用法：/csgo 评论 <比赛ID> [条数]，例如 /csgo 评论 2399984 5")
                    return
                yield event.plain_result(await self._cmd_comments(param))
                return

            if sub_cmd in ("比分", "战绩"):
                if not param:
                    yield event.plain_result("用法：/csgo 比分 <队伍名>，例如 /csgo 比分 Faze")
                    return
                yield event.plain_result(await self._cmd_score(param, umo))
                return

            if sub_cmd in ("队伍订阅", "订阅队伍"):
                if not param:
                    yield event.plain_result("用法：/csgo 队伍订阅 <队伍名>，例如 /csgo 队伍订阅 Faze")
                    return
                yield event.plain_result(await self._cmd_sub_team(umo, param))
                return

            if sub_cmd in ("赛事订阅", "订阅赛事"):
                if not param:
                    yield event.plain_result("用法：/csgo 赛事订阅 <赛事名>，例如 /csgo 赛事订阅 IEM 成都")
                    return
                yield event.plain_result(await self._cmd_sub_event(umo, param))
                return

            if sub_cmd in ("队伍取消", "取消队伍"):
                if not param:
                    yield event.plain_result("用法：/csgo 队伍取消 <队伍名>")
                    return
                yield event.plain_result(self._cmd_unsub_team(umo, param))
                return

            if sub_cmd in ("赛事取消", "取消赛事"):
                if not param:
                    yield event.plain_result("用法：/csgo 赛事取消 <赛事名>")
                    return
                yield event.plain_result(self._cmd_unsub_event(umo, param))
                return

            if sub_cmd in ("我的订阅", "订阅列表", "订阅"):
                yield event.plain_result(self._cmd_my_subs(umo))
                return

            yield event.plain_result(
                f"未知指令：{sub_cmd}\n\n" + self._help_text()
            )
        except Exception as exc:
            logger.exception(f"[wmpvp_csgo] 指令 {sub_cmd} 执行失败")
            yield event.plain_result(f"❌ 指令执行异常：{exc}")

    def _help_text(self) -> str:
        return (
            "🎯 完美世界电竞 CSGO 赛事助手\n"
            "━━━━━━━━━━━━━━━━\n"
            "【查询】\n"
            "  /csgo 赛程 [今天|明天|日期] 赛事列表\n"
            "  /csgo 详情 <比赛ID>        高保真数据卡（含支持率 / VRS / 评论数）\n"
            "  /csgo 评论 <比赛ID> [条数]  热门评论（按点赞排序）\n"
            "  /csgo 数据源 [auto|5eplay|wanmei_data|wmpvp]  查看/切换本群数据源\n"
            "  /csgo 比分 <队伍>          队伍相关比赛\n"
            "【订阅】\n"
            "  /csgo 队伍订阅 <队伍>      支持缩写/别名（Faze、LVG）\n"
            "  /csgo 赛事订阅 <赛事>      支持带空格全名/模糊匹配\n"
            "  开赛前 15 分钟自动推「赛前预告」：谁打谁 + 支持率 + VRS + 热评\n"
            "  /csgo 队伍取消 <队伍>\n"
            "  /csgo 赛事取消 <赛事>\n"
            "  /csgo 我的订阅             仅显示当前会话\n"
            "【调试】\n"
            "  /csgo 测试 渲染            渲染赛前/逐图/总数据全部卡片（验证三级降级）\n"
            "  /csgo 诊断                 查看轮询/订阅/渲染状态，定位「不推送」\n"
            "  /csgo 测试 推送            向当前会话发一条主动推送\n"
            "  /csgo 测试 API             探测接口连通性\n"
            "━━━━━━━━━━━━━━━━\n"
            "订阅与推送严格按会话隔离，互不串群。"
        )

    # ------------------------------------------------------------------ #
    # 查询类指令
    # ------------------------------------------------------------------ #

    async def _cmd_schedule(self, day_param: str = "") -> str:
        target, label = self._parse_day(day_param)
        try:
            matches = await self.client.fetch_match_list(target)
        except ApiError as exc:
            return self._api_fail_text(exc)

        if not matches:
            return f"📭 {label}（{target.isoformat()}）没有赛事安排。"

        live = [m for m in matches if m.get("status") == STATUS_LIVE]
        upcoming = [m for m in matches if m.get("status") == STATUS_UPCOMING]
        finished = [m for m in matches if m.get("status") == STATUS_FINISHED]
        upcoming.sort(key=lambda m: m.get("start_time") or 0)
        finished.sort(key=lambda m: m.get("start_time") or 0, reverse=True)

        picked = live[:6] + upcoming[:8] + finished[:4]
        head = f"🏆 {label}赛程（{target.isoformat()}）共 {len(matches)} 场"
        stat = f"🔴{len(live)} 进行中 ｜ 🕒{len(upcoming)} 未开始 ｜ ✅{len(finished)} 已结束"
        lines = [head, stat, "━━━━━━━━━━━━━━━━"]
        for idx, m in enumerate(picked, 1):
            lines.append(self._match_line(m, idx))
        if len(picked) < len(matches):
            lines.append(f"… 另有 {len(matches) - len(picked)} 场未显示")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("用 /csgo 详情 <比赛ID> 查看数据卡 ｜ /csgo 赛程 明天")
        return "\n".join(lines)

    @staticmethod
    def _parse_day(day_param: str) -> Tuple[date, str]:
        text = (day_param or "").strip()
        today = date.today()
        if not text or text in ("今天", "今日"):
            return today, "今日"
        if text in ("明天", "明日"):
            return today + timedelta(days=1), "明日"
        if text in ("昨天", "昨日"):
            return today - timedelta(days=1), "昨日"
        if text in ("后天",):
            return today + timedelta(days=2), "后日"
        try:
            return datetime.strptime(text, "%Y-%m-%d").date(), text
        except Exception:
            return today, "今日"

    def _match_line(self, m: Dict[str, Any], idx: int = 0) -> str:
        icon = {STATUS_LIVE: "🔴", STATUS_UPCOMING: "🕒", STATUS_FINISHED: "✅"}.get(
            m.get("status"), "🕒")
        prefix = f"[{idx}] " if idx else ""
        head = f"{prefix}{icon} {m.get('team_a')} {m.get('score_a')} : {m.get('score_b')} {m.get('team_b')}"
        meta_bits = []
        if m.get("event_name"):
            meta_bits.append(m["event_name"])
        if m.get("status") == STATUS_UPCOMING and m.get("start_time"):
            meta_bits.append(_fmt_ts(m["start_time"]))
        if m.get("bo"):
            meta_bits.append(f"BO{m['bo']}")
        live_map = self._current_map_label(m)
        if live_map:
            meta_bits.append(live_map)
        tail = f"\n    {' · '.join(meta_bits)}" if meta_bits else ""
        mid = f"\n    ID: {m.get('match_id')}" if m.get("match_id") else ""
        return head + tail + mid

    @staticmethod
    def _current_map_label(m: Dict[str, Any]) -> str:
        maps = m.get("maps") or []
        live = [x for x in maps if x.get("status") == STATUS_LIVE]
        if live:
            x = live[0]
            return f"图{int(x.get('map_index', 0)) + 1} {x.get('map_name')} {x.get('score_a')}:{x.get('score_b')}"
        return ""

    async def _cmd_detail(self, event: AstrMessageEvent, match_id: str):
        match_id = match_id.strip()
        client = self._client_for(getattr(event, "unified_msg_origin", "") or "")
        try:
            match = await client.fetch_match_detail(match_id)
        except ApiError as exc:
            yield event.plain_result(self._api_fail_text(exc))
            return

        if not match:
            yield event.plain_result(
                f"❓ 未找到比赛 ID：{match_id}\n可先用 /csgo 赛程 查看可用比赛。"
            )
            return

        await self._attach_extras(match)
        view = await self._build_view_with_images(match)
        path = await self._render_html_to_image(self._tmpl, view)
        if path:
            tip = f"🆚 {view['team_a']} {view['score_a']} : {view['score_b']} {view['team_b']}"
            if view.get("comment_count"):
                tip += f" ｜ 💬 {view['comment_count']} 条评论（/csgo 评论 {match_id}）"
            yield event.plain_result(tip)
            yield event.image_result(path)
            self._schedule_cleanup(path)
        else:
            yield event.plain_result(self._build_text_card(view))

    async def _attach_extras(self, match: Dict[str, Any]) -> None:
        """补齐支持率 / VRS / 评论数（仅 wmpvp 源可用，失败静默跳过）。

        这三个接口都在 gwapi 下，且只认 wmpvp 的裸数字 ID；5EPlay 的
        `5e:csgo_mc_xxx` 取不到对应数据，直接跳过，不额外发请求。
        """
        mid = str(match.get("match_id") or "")
        if not mid:
            return
        gw = self._gw_source()
        if gw is None:
            return

        # 5EPlay 的 ID 与完美系不通，需要按开赛时间 + 队名找同一场比赛
        if mid.startswith(f"{SOURCE_5E}:"):
            resolved = await self._resolve_wmpvp_id(match, gw)
            if not resolved:
                return
            mid = resolved

        tasks = [gw.fetch_comment_count(mid)]
        # VRS 是"如果赢/如果输"的预测，比赛结束后已无意义（接口会返回 0 或残留值）
        want_vrs = match.get("status") != STATUS_FINISHED
        if want_vrs:
            tasks.append(gw.fetch_rank_predict(mid))
        # 支持率只在完美系的详情里，5E 源需要额外取一次
        need_support = not (match.get("support_rate") or {})
        if need_support:
            tasks.append(gw.fetch_match_detail(mid))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        cnt = results[0]
        if isinstance(cnt, int) and cnt:
            match["comment_count"] = cnt

        idx = 1
        if want_vrs:
            rp = results[idx] if len(results) > idx else None
            idx += 1
            if isinstance(rp, dict) and rp:
                match["rank_predict"] = rp

        if need_support and len(results) > idx:
            det = results[idx]
            if isinstance(det, dict):
                sr = self._orient_support(det, match)
                if sr:
                    match["support_rate"] = sr

    @staticmethod
    def _orient_support(detail: Dict[str, Any],
                        match: Dict[str, Any]) -> Dict[str, Any]:
        """把完美系详情的支持率对齐到当前比赛的 A/B 顺序。

        跨源匹配时两队顺序可能与完美系相反，直接套用会把百分比挂错队。
        """
        sr = detail.get("support_rate") or {}
        if not sr:
            return {}
        same_order = (difflib.SequenceMatcher(None, _norm(detail.get("team_a") or ""),
                                             _norm(match.get("team_a") or "")).ratio()
                      >= 0.6)
        if same_order:
            return sr
        return {"a": sr.get("b"), "b": sr.get("a"),
                "votes_a": sr.get("votes_b"), "votes_b": sr.get("votes_a"),
                "votes_total": sr.get("votes_total")}

    async def _resolve_wmpvp_id(self, match: Dict[str, Any], gw: Any) -> str:
        """把 5EPlay 的比赛映射到完美电竞的同场比赛 ID。

        两个源没有共享 ID，只能靠「开赛时间接近 + 队名相似」匹配。匹配不到
        就返回空串——宁可少展示，也不要把别场的数据挂到这场上。
        """
        ts = _safe_int(match.get("start_time"))
        if not ts:
            return ""
        try:
            day = date.fromtimestamp(ts)
            rows = await gw.fetch_match_list(day)
        except Exception:
            return ""

        key_a = _norm(match.get("team_a") or "")
        key_b = _norm(match.get("team_b") or "")
        best, best_score = "", 0.0
        for m in rows:
            other_ts = _safe_int(m.get("start_time"))
            if not other_ts or abs(other_ts - ts) > 900:      # 15 分钟内
                continue
            oa = _norm(m.get("team_a") or "")
            ob = _norm(m.get("team_b") or "")
            # 两队顺序可能相反，取正序/倒序里更像的那个组合
            direct = (difflib.SequenceMatcher(None, key_a, oa).ratio()
                      + difflib.SequenceMatcher(None, key_b, ob).ratio()) / 2
            cross = (difflib.SequenceMatcher(None, key_a, ob).ratio()
                     + difflib.SequenceMatcher(None, key_b, oa).ratio()) / 2
            score = max(direct, cross)
            if score > best_score:
                best, best_score = str(m.get("match_id") or ""), score
        return best if best_score >= 0.8 else ""

    async def _cmd_comments(self, param: str) -> str:
        """热门评论列表（按点赞数排序）。"""
        parts = param.split()
        mid = parts[0].strip()
        limit = COMMENT_PAGE_SIZE
        if len(parts) > 1:
            limit = max(1, min(20, _safe_int(parts[1], COMMENT_PAGE_SIZE)))

        gw = self._gw_source()
        if gw is None:
            return "❌ 评论接口仅在完美电竞网关（wmpvp）下可用。"

        # 5E 的比赛先映射到完美系的同场比赛
        if mid.startswith(f"{SOURCE_5E}:"):
            try:
                rows = await self.client.fetch_match_list()
            except ApiError:
                rows = []
            picked = next((m for m in rows if str(m.get("match_id")) == mid), None)
            resolved = await self._resolve_wmpvp_id(picked, gw) if picked else ""
            if not resolved:
                return ("❌ 这场比赛在完美电竞里没找到对应记录，暂无评论数据。\n"
                        "可改用 /csgo 赛程 里纯数字 ID 的 wmpvp 比赛。")
            mid = resolved

        try:
            total, items = await asyncio.gather(
                gw.fetch_comment_count(mid),
                gw.fetch_comments(mid, limit),
                return_exceptions=True,
            )
        except Exception as exc:
            return f"❌ 评论获取失败：{exc}"
        if isinstance(items, Exception):
            return f"❌ 评论获取失败：{items}"
        if not items:
            return f"💬 比赛 {mid} 暂无评论。"

        total = total if isinstance(total, int) and total else len(items)
        lines = [f"💬 比赛 {mid} 热评（共 {total} 条，按点赞排序）", "━━━━━━━━━━━━━━━━"]
        for i, c in enumerate(items, 1):
            head = f"{i}. {c['user']}"
            bits = []
            if c.get("region"):
                bits.append(c["region"])
            if c.get("time"):
                bits.append(c["time"])
            if bits:
                head += f"（{'，'.join(bits)}）"
            lines.append(head)
            lines.append(f"   {c['content'][:120]}")
            tail = f"   👍 {c['likes']}"
            if c.get("replies"):
                tail += f" ｜ 💭 {c['replies']} 回复"
            if c.get("has_img"):
                tail += " ｜ 🖼 含图"
            lines.append(tail)
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("数据来源：完美世界电竞社区")
        return "\n".join(lines)

    async def _cmd_score(self, team_input: str, umo: str = "") -> str:
        try:
            matches = await self._client_for(umo).fetch_match_list()
        except ApiError as exc:
            return self._api_fail_text(exc)

        canonical, candidates = self._resolve_team(team_input, matches)
        if candidates and not canonical:
            names = "、".join(candidates[:6])
            return f"🤔 没匹配到「{team_input}」，你是不是想找：{names}\n请用更完整的队名重试。"

        target = canonical or team_input
        key = _norm(target)
        hits = [m for m in matches
                if key in _norm(m.get("team_a")) or key in _norm(m.get("team_b"))
                or _norm(target) == _norm(m.get("team_a")) or _norm(target) == _norm(m.get("team_b"))]

        if not hits:
            return (f"📭 没有找到「{target}」的相关比赛。\n"
                    f"可用 /csgo 赛程 查看当前赛事，或确认队伍名是否正确。")

        hits = sorted(hits, key=lambda m: m.get("start_time") or 0, reverse=True)[:8]
        lines = [f"📊 {target} 相关比赛（{len(hits)} 场）", "━━━━━━━━━━━━━━━━"]
        for m in hits:
            icon = {STATUS_LIVE: "🔴", STATUS_UPCOMING: "🕒", STATUS_FINISHED: "✅"}.get(
                m.get("status"), "🕒")
            when = _fmt_ts(m.get("start_time")) if m.get("status") == STATUS_UPCOMING else STATUS_TEXT.get(m.get("status"), "")
            lines.append(f"{icon} {m.get('team_a')} {m.get('score_a')} : {m.get('score_b')} {m.get('team_b')}")
            meta = " · ".join(x for x in [m.get("event_name"), when] if x)
            if meta:
                lines.append(f"    {meta}")
            if m.get("match_id"):
                lines.append(f"    ID: {m.get('match_id')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 订阅类指令
    # ------------------------------------------------------------------ #

    async def _cmd_sub_team(self, umo: str, team_input: str) -> str:
        if self.subs.count(umo) >= self.max_subs:
            return f"⚠️ 当前会话订阅数已达上限（{self.max_subs}），请先取消部分订阅。"

        matches: List[Dict[str, Any]] = []
        try:
            matches = await self.client.fetch_match_list()
        except ApiError:
            matches = []

        canonical, candidates = self._resolve_team(team_input, matches)
        if candidates and not canonical:
            names = "、".join(candidates[:6])
            return f"🤔 没匹配到「{team_input}」，你是不是想找：{names}"

        target = canonical or team_input
        added, name = self.subs.add_team(umo, target)
        if not added:
            return f"ℹ️ 本会话已订阅过「{name}」，无需重复订阅。"
        return (f"✅ 已订阅队伍：{name}\n"
                f"该队伍比赛开赛 / 地图结束 / 整场结束时会推送到本会话。\n"
                f"查看：/csgo 我的订阅")

    async def _event_calendar(self, force: bool = False, umo: str = "") -> List[str]:
        """近期赛事全名（过去 7 天 ~ 未来 30 天），缓存 5 分钟。

        列表接口是按天的，38 天要发 38 次请求，所以并发拉取并整体缓存，
        避免每次订阅都打满接口。
        """
        client = self._client_for(umo) if umo else self.client
        cache_key = getattr(client, "source_id", "") or "default"

        now = time.time()
        cached = self._event_cache.get(cache_key)
        if not force and cached and (now - cached[0]) < 300:
            return cached[1]

        today = date.today()
        days = [today + timedelta(days=offset) for offset in range(-7, 31)]
        sem = asyncio.Semaphore(8)

        async def _one(d: date) -> List[Dict[str, Any]]:
            async with sem:
                try:
                    return await client.fetch_match_list(d)
                except ApiError:
                    return []

        results = await asyncio.gather(*(_one(d) for d in days), return_exceptions=True)
        names: set = set()
        for rows in results:
            if isinstance(rows, Exception) or not rows:
                continue
            for m in rows:
                name = str(m.get("event_name") or "").strip()
                if name:
                    names.add(name)
        ordered = sorted(names)
        self._event_cache[cache_key] = (time.time(), ordered)
        return ordered

    @staticmethod
    def _match_event_names(keyword: str, names: List[str]) -> List[str]:
        """第一步：不区分大小写的子串匹配。"""
        key = _norm(keyword)
        if not key:
            return []
        return [n for n in names if key in _norm(n)]

    def _get_llm_provider(self, umo: str = "") -> Any:
        """插件独立 LLM 优先，取不到再退回 AstrBot 当前对话模型。"""
        custom = str(self._cfg("llm_provider_id", "") or "").strip()
        if custom:
            try:
                provider = self.context.get_provider_by_id(custom)
                if provider is not None:
                    return provider
                logger.warning(f"[wmpvp_csgo] 未找到 LLM 提供商 {custom!r}，回退默认模型")
            except Exception as exc:
                logger.warning(f"[wmpvp_csgo] 获取 LLM 提供商 {custom!r} 失败：{exc}")
        try:
            return self.context.get_using_provider(umo=umo) if umo \
                else self.context.get_using_provider()
        except TypeError:
            try:
                return self.context.get_using_provider()
            except Exception:
                return None
        except Exception:
            return None

    async def _llm_complete(self, provider: Any, prompt: str) -> str:
        fn = getattr(provider, "text_chat", None)
        if not callable(fn):
            return ""
        try:
            resp = await fn(prompt=prompt)
        except TypeError:
            resp = await fn(prompt)
        if resp is None:
            return ""
        text = getattr(resp, "completion_text", None)
        if text:
            return str(text)
        if isinstance(resp, str):
            return resp
        return str(getattr(resp, "text", "") or "")

    async def _llm_match_events(self, keyword: str, names: List[str],
                                umo: str = "") -> List[str]:
        """第二步：代码没匹配到时，用 LLM 做语义兜底（3 秒超时，失败静默）。"""
        if not names:
            return []
        provider = self._get_llm_provider(umo)
        if provider is None:
            return []

        sample = names[:80]          # 控制 prompt 体积
        prompt = (
            f'用户想要订阅与“{keyword}”相关的电竞赛事。以下是近期真实的赛事列表：\n'
            f'{json.dumps(sample, ensure_ascii=False)}\n'
            "请从中挑选出用户可能想订阅的具体赛事全名。如果没有任何相关的，请返回空数组。\n"
            '请严格以 JSON 数组格式输出，例如：["赛事名1", "赛事名2"]。不要包含任何其他解释文字。'
        )
        try:
            raw = await asyncio.wait_for(self._llm_complete(provider, prompt), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.info(f"[wmpvp_csgo] LLM 语义匹配跳过：{type(exc).__name__}")
            return []

        text = str(raw or "")
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
        if not isinstance(data, list):
            return []

        # 只接受确实在候选列表里的名字，避免模型编造
        valid = {n: n for n in names}
        picked: List[str] = []
        for item in data:
            name = str(item).strip()
            if name in valid and name not in picked:
                picked.append(name)
        return picked

    async def _cmd_sub_event(self, umo: str, event_input: str) -> str:
        if self.subs.count(umo) >= self.max_subs:
            return f"⚠️ 当前会话订阅数已达上限（{self.max_subs}），请先取消部分订阅。"

        keyword = event_input.strip()
        names = await self._event_calendar(umo=umo)
        matched = self._match_event_names(keyword, names)
        if not matched:
            matched = await self._llm_match_events(keyword, names, umo)

        # 无论是否匹配到，都把「原始关键词」写入订阅（匹配到的全名另存，用于命中）
        added, name = self.subs.add_event(umo, keyword, matched)
        if not added:
            return f"ℹ️ 本会话已订阅过「{name}」，无需重复订阅。"

        if matched:
            shown = "、".join(matched[:6])
            more = f" 等 {len(matched)} 个" if len(matched) > 6 else ""
            return (f"✅ 已为本群订阅赛事关键词：`{keyword}`\n"
                    f"📌 近期匹配到的赛事：{shown}{more}\n"
                    f"以上赛事的后续动态将推送到本会话。")
        return (f"✅ 已为本群订阅赛事关键词：`{keyword}`\n"
                f"🔍 当前近期赛程中暂未发现包含该关键词的赛事。\n"
                f"一旦有新赛事发布，将自动推送。")

    def _cmd_unsub_team(self, umo: str, team_input: str) -> str:
        if self.subs.remove_team(umo, team_input):
            return f"🗑️ 已取消队伍订阅：{team_input}"
        # 尝试别名匹配
        for item in self.subs.get(umo)["teams"]:
            if _norm(team_input) in _norm(item.get("name")):
                self.subs.remove_team(umo, item.get("name"))
                return f"🗑️ 已取消队伍订阅：{item.get('name')}"
        return f"❓ 本会话没有订阅「{team_input}」，可用 /csgo 我的订阅 查看。"

    def _cmd_unsub_event(self, umo: str, event_input: str) -> str:
        if self.subs.remove_event(umo, event_input):
            return f"🗑️ 已取消赛事订阅：{event_input}"
        for item in self.subs.get(umo)["events"]:
            if _norm(event_input) in _norm(item.get("name")):
                self.subs.remove_event(umo, item.get("name"))
                return f"🗑️ 已取消赛事订阅：{item.get('name')}"
        return f"❓ 本会话没有订阅「{event_input}」，可用 /csgo 我的订阅 查看。"

    def _cmd_my_subs(self, umo: str) -> str:
        snapshot = self.subs.get(umo)
        teams = snapshot["teams"]
        events = snapshot["events"]
        if not teams and not events:
            return ("📭 本会话还没有任何订阅。\n"
                    "试试：/csgo 队伍订阅 Faze 或 /csgo 赛事订阅 IEM 成都")

        lines = ["📌 本会话订阅", "━━━━━━━━━━━━━━━━"]
        if teams:
            lines.append(f"队伍（{len(teams)}）")
            for it in teams:
                lines.append(f"  · {it.get('name')}")
        if events:
            lines.append(f"赛事（{len(events)}）")
            for it in events:
                keyword = str(it.get("name") or "")
                matched = [m for m in (it.get("matched") or []) if m]
                if not matched:
                    lines.append(f"  · {keyword}")
                    continue
                # 有全名就展示全名，关键词放在后面括号里（取消时两种都能用）
                lines.append(f"  · {matched[0]}")
                for extra in matched[1:]:
                    lines.append(f"    {extra}")
                if _norm(keyword) not in {_norm(m) for m in matched}:
                    lines.append(f"    （关键词：{keyword}）")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("取消：/csgo 队伍取消 <队伍> ｜ /csgo 赛事取消 <赛事>")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 名称解析
    # ------------------------------------------------------------------ #

    def _resolve_team(self, team_input: str, matches: List[Dict[str, Any]]
                      ) -> Tuple[Optional[str], List[str]]:
        """返回 (规范队名, 候选列表)。候选列表非空且规范名为 None 表示歧义。"""
        raw = (team_input or "").strip()
        if not raw:
            return None, []
        key = _norm(raw)

        known = sorted({m.get("team_a") for m in matches if m.get("team_a")}
                       | {m.get("team_b") for m in matches if m.get("team_b")})

        # 1) 别名表
        if key in TEAM_ALIASES:
            alias_target = TEAM_ALIASES[key]
            for name in known:
                if _norm(name) == _norm(alias_target) or _norm(alias_target) in _norm(name):
                    return name, []
            return alias_target, []

        # 2) 精确 / 包含
        for name in known:
            if _norm(name) == key:
                return name, []
        contains = [n for n in known if key and key in _norm(n)]
        if len(contains) == 1:
            return contains[0], []
        if len(contains) > 1:
            return None, contains

        # 3) 模糊
        close = difflib.get_close_matches(key, [_norm(n) for n in known], n=3, cutoff=0.72)
        if close:
            mapping = {_norm(n): n for n in known}
            return mapping.get(close[0]), []

        # 4) 接口没数据时，直接把输入当关键词
        if not known:
            return raw, []
        return None, known[:6]

    @staticmethod
    def _fuzzy_pick(text: str, pool: List[str]) -> Optional[str]:
        key = _norm(text)
        if not key:
            return None
        for name in pool:
            if _norm(name) == key:
                return name
        contains = [n for n in pool if key in _norm(n)]
        if len(contains) == 1:
            return contains[0]
        if contains:
            return min(contains, key=len)
        # 反向包含：输入比赛事名更长
        rev = [n for n in pool if _norm(n) and _norm(n) in key]
        if rev:
            return max(rev, key=len)
        close = difflib.get_close_matches(key, [_norm(n) for n in pool], n=1, cutoff=0.6)
        if close:
            mapping = {_norm(n): n for n in pool}
            return mapping.get(close[0])
        return None

    # ------------------------------------------------------------------ #
    # 测试指令集
    # ------------------------------------------------------------------ #

    async def _handle_test(self, event: AstrMessageEvent, umo: str, param: str):
        test_type = (param or "").strip().lower()

        if not test_type:
            yield event.plain_result(
                "🧪 测试指令集\n"
                "  /csgo 测试 渲染   渲染赛前/逐图/总数据全部卡片（无数据时用 Mock）\n"
                "  /csgo 诊断       定位「不推送 / 没有图」的原因\n"
                "  /csgo 测试 推送   向当前会话主动推送一条消息\n"
                "  /csgo 测试 API    探测赛事接口连通性"
            )
            return

        if self.test_admin_only and not self._is_admin(event):
            yield event.plain_result("🔒 测试指令仅管理员可用。")
            return

        if test_type in ("渲染", "render"):
            async for r in self._test_render(event):
                yield r
        elif test_type in ("推送", "push"):
            async for r in self._test_push(event, umo):
                yield r
        elif test_type in ("api", "接口"):
            async for r in self._test_api(event, umo):
                yield r
        else:
            yield event.plain_result("未知测试类型，支持：渲染、推送、API")

    async def _test_render(self, event: AstrMessageEvent):
        """随机抽比赛，把各阶段卡片都渲染一遍：赛前 / 逐图 / 总数据。

        赛前预告需要 VRS（只有未结束的比赛才有），逐图与总数据需要完整战绩，
        一场比赛不可能同时满足，所以分别抽两场。
        """
        started = time.time()
        ended, upcoming, source = await self._pick_render_samples()
        if not ended:
            yield event.plain_result("❌ 没能取到可用的比赛数据。")
            return

        variants = await self._build_render_variants(ended, upcoming)

        rendered: List[Tuple[str, str]] = []
        for label, view in variants:
            path = await self._render_html_to_image(self._tmpl, view)
            if path:
                rendered.append((label, path))

        elapsed = int((time.time() - started) * 1000)
        head = (f"🧪 测试渲染（{elapsed}ms，共 {len(rendered)}/{len(variants)} 张）\n"
                f"{source}")
        if upcoming:
            head += (f"\n赛前样本：{upcoming.get('team_a')} vs {upcoming.get('team_b')}")
        head += (f"\n战绩样本：{ended.get('team_a')} {ended.get('score_a')} : "
                 f"{ended.get('score_b')} {ended.get('team_b')}")

        if not rendered:
            view = variants[-1][1] if variants else self._build_view(ended)
            yield event.plain_result(
                head + "\n\n❌ 测试失败：图片渲染（含多级降级）彻底失效，已回退纯文本卡片：\n\n"
                + self._build_text_card(view))
            return

        yield event.plain_result(head)
        for label, path in rendered:
            yield event.plain_result(f"🖼 {label}")
            yield event.image_result(path)
            self._schedule_cleanup(path)

    async def _pick_render_samples(
            self) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str]:
        """返回 (已结束比赛, 未开始比赛, 来源说明)。"""
        ended: Optional[Dict[str, Any]] = None
        upcoming: Optional[Dict[str, Any]] = None
        source = ""

        for offset in (0, 1, 2):
            day = date.today() - timedelta(days=offset)
            try:
                rows = await self.client.fetch_match_list(day)
            except Exception:
                continue

            if not ended:
                finished = [m for m in rows
                            if m.get("status") == STATUS_FINISHED and m.get("match_id")]
                for pick in random.sample(finished, min(4, len(finished))):
                    try:
                        detail = await self.client.fetch_match_detail(str(pick["match_id"]))
                    except Exception:
                        detail = None
                    if detail and detail.get("maps"):
                        ended = detail
                        source = f"真实比赛（{day.isoformat()} 随机抽取）"
                        break

            if not upcoming:
                todo = [m for m in rows
                        if m.get("status") == STATUS_UPCOMING and m.get("match_id")
                        and _named(m)]
                if todo:
                    upcoming = dict(random.choice(todo))
                    source = source or f"真实比赛（{day.isoformat()} 随机抽取）"

            if ended and upcoming:
                break

        # 赛前预告要 VRS/支持率/热评，这些只有完美系有，优先直接取 gw 的赛程
        gw = self._gw_source()
        if gw is not None:
            for offset in (0, 1):
                try:
                    rows = await gw.fetch_match_list(date.today() + timedelta(days=offset))
                except Exception:
                    continue
                todo = [m for m in rows
                        if m.get("status") == STATUS_UPCOMING and _named(m)]
                if todo:
                    upcoming = dict(random.choice(todo))
                    break

        if not ended:
            ended = self.client.normalize_match(MOCK_MATCH_DATA)
            source = "内置 Mock 数据"
        return ended, upcoming, source

    async def _build_render_variants(
            self, match: Dict[str, Any],
            upcoming: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """构造各阶段卡片的视图：赛前预告 / 每张图结束 / 总数据。"""
        out: List[Tuple[str, Dict[str, Any]]] = []

        # 1) 赛前预告：优先用真实未开始的比赛（才有 VRS），否则用当前比赛的壳
        pre = dict(upcoming or match)
        pre["maps"] = []
        pre["players"] = []          # 赛前本来就没有选手数据，保持真实
        pre["status"] = STATUS_UPCOMING
        pre["score_a"] = 0
        pre["score_b"] = 0
        await self._attach_extras(pre)

        comments: List[Dict[str, Any]] = []
        gw = self._gw_source()
        mid = str(pre.get("match_id") or "")
        if gw is not None and mid:
            target = mid
            if mid.startswith(f"{SOURCE_5E}:"):
                target = await self._resolve_wmpvp_id(pre, gw)
            if target:
                try:
                    comments = await gw.fetch_comments(target, 3)
                except Exception:
                    comments = []

        pre_view = await self._build_view_with_images(pre)
        pre_view["preview"] = True
        pre_view["comments"] = comments
        start_ts = _safe_int(pre.get("start_time"))
        pre_view["countdown"] = (self._countdown_text(start_ts)
                                 if start_ts and start_ts > time.time() else "10 分钟后开始")
        out.append(("赛前预告", pre_view))

        # 2) 逐图结束卡（最多 3 张，避免刷屏）
        all_maps = match.get("maps") or []
        for i, item in enumerate(all_maps[:3]):
            single = dict(match)
            single["maps"] = [item]
            # 大比分用「打到这张图时」的累计值，而不是最终比分
            done = all_maps[:i + 1]
            single["score_a"] = sum(1 for x in done
                                    if _safe_int(x.get("score_a")) > _safe_int(x.get("score_b")))
            single["score_b"] = sum(1 for x in done
                                    if _safe_int(x.get("score_b")) > _safe_int(x.get("score_a")))
            single["status"] = (STATUS_FINISHED if i == len(all_maps) - 1 else STATUS_LIVE)
            idx = _safe_int(item.get("map_index"))
            rows = (match.get("map_players") or {}).get(idx)
            if rows:
                single["players"] = rows
            out.append((f"图{idx + 1} 结束 · {item.get('map_name') or ''}".strip(),
                        await self._build_view_with_images(single)))

        # 3) 总数据：全部地图 + 双方选手
        out.append(("总数据", await self._build_view_with_images(match)))
        return out

    async def _pick_sample_match(self) -> Tuple[Dict[str, Any], str]:
        """测试渲染用的样本：优先随机抽一场真实「已结束」比赛，拿不到才用 Mock。

        已结束的比赛才有逐图比分和 5v5 选手数据，能完整验证解析 + 渲染链路。
        """
        for offset in (0, 1, 2):
            day = date.today() - timedelta(days=offset)
            try:
                rows = await self.client.fetch_match_list(day)
            except Exception:
                continue
            finished = [m for m in rows
                        if m.get("status") == STATUS_FINISHED and m.get("match_id")]
            if not finished:
                continue
            for pick in random.sample(finished, min(3, len(finished))):
                try:
                    detail = await self.client.fetch_match_detail(str(pick["match_id"]))
                except Exception:
                    detail = None
                if detail and detail.get("maps"):
                    return detail, f"真实比赛（{day.isoformat()} 随机抽取）"
        return self.client.normalize_match(MOCK_MATCH_DATA), "内置 Mock 数据"

    async def _test_push(self, event: AstrMessageEvent, umo: str):
        if not umo:
            yield event.plain_result("❌ 无法获取当前会话标识（unified_msg_origin 为空）。")
            return
        text = f"🔔 主动推送测试成功！\n目标会话：{umo}\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}"
        try:
            chain = self._build_chain(text)
            await self.context.send_message(umo, chain)
            yield event.plain_result("✅ 已发起独立推送，请检查是否收到另一条消息。")
        except Exception as exc:
            logger.exception("[wmpvp_csgo] 测试推送失败")
            yield event.plain_result(f"❌ 推送异常：{exc}")

    async def _test_api(self, event: AstrMessageEvent, umo: str):
        # 冷却，避免频繁触发风控
        now = time.time()
        last = self._test_api_last.get(umo, 0)
        if self.test_api_cooldown > 0 and now - last < self.test_api_cooldown:
            wait = int(self.test_api_cooldown - (now - last)) + 1
            yield event.plain_result(f"⏳ 测试 API 冷却中，请 {wait} 秒后再试。")
            return
        self._test_api_last[umo] = now

        if httpx is None:
            yield event.plain_result("❌ 缺少 httpx 依赖，请先 pip install httpx")
            return

        results = await self.client.probe()
        lines = ["🧪 接口探测（多数据源）", "━━━━━━━━━━━━━━━━"]
        all_ok = True
        for item in results:
            flag = "✅" if item["ok"] else "❌"
            all_ok = all_ok and item["ok"]
            lines.append(f"{flag} {item['label']}  {item['status']}  {item['elapsed_ms']}ms")
            lines.append(f"    {item['url']}")
            if item.get("params"):
                qs = "&".join(f"{k}={v}" for k, v in item["params"].items())
                lines.append(f"    ?{qs}")
            lines.append(f"    结果: {item.get('note') or '—'}")
            body = (item.get("body") or "").replace("\n", " ")
            if not item["ok"] and body:
                lines.append(f"    响应前200字: {body[:200]}")
        lines.append("━━━━━━━━━━━━━━━━")
        if all_ok:
            lines.append("全部接口正常，可直接查询与推送。")
        else:
            lines.append("⚠️ 存在异常接口；其余源仍可正常工作，请对照状态码排查。")
        yield event.plain_result("\n".join(lines))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            role = getattr(event, "role", None)
            if role is not None and str(role).lower() in ("admin", "owner", "administrator"):
                return True
            return bool(getattr(event, "is_admin", lambda: False)())
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 渲染：三级降级
    # ------------------------------------------------------------------ #

    async def _render_html_to_image(self, tmpl: str, data: Dict[str, Any]) -> Optional[str]:
        mode = self.render_mode
        tries: List[str] = []

        if mode in ("auto", "astrbot", "html_render"):
            try:
                path = await self._render_via_astrbot(tmpl, data)
                if path:
                    self._diag["render_last"] = "html_render ✅"
                    return path
                tries.append("html_render: 返回空")
            except Exception as exc:
                tries.append(f"html_render: {type(exc).__name__} {exc}")
                logger.warning(f"[wmpvp_csgo] html_render 失败: {exc}")

        if mode in ("auto", "playwright"):
            try:
                path = await self._render_via_playwright(tmpl, data)
                if path:
                    self._diag["render_last"] = "Playwright ✅"
                    return path
                tries.append("Playwright: 返回空（浏览器可能未安装）")
            except Exception as exc:
                tries.append(f"Playwright: {type(exc).__name__} {exc}")
                logger.warning(f"[wmpvp_csgo] Playwright 渲染失败: {exc}")

        try:
            path = await self._render_via_chrome(tmpl, data)
            if path:
                self._diag["render_last"] = "Chrome ✅"
                return path
            tries.append("Chrome: 未找到或未出图")
        except Exception as exc:
            tries.append(f"Chrome: {type(exc).__name__} {exc}")
            logger.warning(f"[wmpvp_csgo] Chrome 兜底渲染失败：{exc}")

        self._diag["render_last"] = " ❌ " + " ｜ ".join(tries)
        logger.warning(f"[wmpvp_csgo] 图片渲染全部失败，回退纯文本：{self._diag['render_last']}")
        return None

    async def _render_via_astrbot(self, tmpl: str, data: Dict[str, Any]) -> Optional[str]:
        renderer = getattr(self, "html_render", None)
        if not callable(renderer):
            # 部分 AstrBot 版本把渲染器挂在 context 上而不是 Star 实例上
            renderer = getattr(self.context, "html_render", None)
        if not callable(renderer):
            return None

        # AstrBot 的 t2i 默认是 JPEG quality=40 + 800px 视口，
        # 结果是又糊又右侧留白。这里显式指定：
        #   type=png            → 无损，文字不糊
        #   viewport_width=720  → 与卡片同宽，full_page 截图不再多出白边
        #   dsf=ultra(1.8x)     → 输出 1296px 宽，聊天里缩放后依然清晰
        options = {
            "type": "png",
            "viewport_width": self.render_width,
            "device_scale_factor_level": "ultra",
            "full_page": True,
        }

        try:
            result = renderer(tmpl, data, options=options)
        except TypeError:
            # 老版本 html_render 可能没有 options 参数
            result = renderer(tmpl, data)
        if asyncio.iscoroutine(result):
            result = await result

        if isinstance(result, (bytes, bytearray)):
            out = self._tmp_path("png")
            out.write_bytes(bytes(result))
            self._owned_temps.add(str(out))
            return str(out)

        if isinstance(result, str):
            if result.startswith(("http://", "https://")):
                # 统一落成本地文件：推送链路的 Image.fromFileSystem 只认本地路径，
                # 直接返回 URL 会让整条消息构造失败，最后只剩纯文字。
                try:
                    status, body, _ = await self.client.fetch_binary(result)
                    if status == 200 and body:
                        out = self._tmp_path("png")
                        out.write_bytes(body)
                        self._owned_temps.add(str(out))
                        return str(out)
                except Exception as exc:
                    logger.warning(f"[wmpvp_csgo] 下载渲染图失败：{exc}")
                return result
            if result.startswith("base64://"):
                out = self._tmp_path("png")
                out.write_bytes(base64.b64decode(result[len("base64://"):]))
                self._owned_temps.add(str(out))
                return str(out)
            if Path(result).exists():
                return result

        return None

    def _find_chrome(self) -> str:
        """找本机 Chromium 系浏览器：显式配置 > PATH > Windows 常见安装路径。"""
        configured = str(self._cfg("chrome_path", "") or "").strip()
        if configured and Path(configured).exists():
            return configured
        for name in ("chrome", "google-chrome", "chromium", "msedge"):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            r"C://Program Files//Google//Chrome//Application//chrome.exe",
            r"C://Program Files (x86)//Google//Chrome//Application//chrome.exe",
            r"C://Program Files (x86)//Microsoft//Edge//Application//msedge.exe",
            r"C://Program Files//Microsoft//Edge//Application//msedge.exe",
            "/usr/bin/google-chrome", "/usr/bin/chromium",
            "/usr/bin/chromium-browser", "/opt/google/chrome/chrome",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        # Playwright 装过浏览器的话，直接复用它的可执行文件（Linux 服务器常见）
        pw_root = Path.home() / ".cache" / "ms-playwright"
        if pw_root.exists():
            for pattern in ("chromium-*/chrome-linux/chrome",
                            "chromium-*/chrome-linux64/chrome",
                            "chromium_headless_shell-*/chrome-linux/headless_shell"):
                found = sorted(pw_root.glob(pattern))
                if found:
                    return str(found[-1])
        return ""

    async def _render_via_chrome(self, tmpl: str,
                                 data: Dict[str, Any]) -> Optional[str]:
        """最后一级兜底：用本机 Chrome/Edge headless 直接把 HTML 截成 PNG。

        不依赖 AstrBot 的 html_render，也不依赖 playwright 包；图片已经内联成
        data URI，本地 file:// 页面离线就能完整渲染。
        """
        exe = self._find_chrome()
        if not exe:
            return None
        html = self._render_template(tmpl, data)
        html_path = self._tmp_path("html")
        html_path.write_text(html, encoding="utf-8")
        out = self._tmp_path("png")

        cmd = [
            exe, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--disable-dev-shm-usage",
            f"--window-size={self.render_width},1300",
            "--virtual-time-budget=6000",
            f"--screenshot={out}",
            html_path.as_uri(),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=25)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None

        if out.exists() and out.stat().st_size > 0:
            self._owned_temps.add(str(out))
            self._owned_temps.add(str(html_path))
            return str(out)
        return None

    async def _render_via_playwright(self, tmpl: str, data: Dict[str, Any]) -> Optional[str]:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            logger.info(f"[wmpvp_csgo] Playwright 不可用: {exc}")
            return None

        html = self._render_template(tmpl, data)
        out = self._tmp_path("png")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = await browser.new_page(
                    viewport={"width": self.render_width, "height": 800},
                    device_scale_factor=2,
                )
                await page.set_content(html, wait_until="load", timeout=15000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass  # 外链图片加载慢，不阻塞截图
                element = await page.query_selector("#card")
                if element is not None:
                    await element.screenshot(path=str(out))
                else:
                    await page.screenshot(path=str(out), full_page=True)
            finally:
                await browser.close()

        if out.exists() and out.stat().st_size > 0:
            self._owned_temps.add(str(out))
            return str(out)
        return None

    def _render_template(self, tmpl: str, data: Dict[str, Any]) -> str:
        if _JinjaTemplate is None:
            raise RuntimeError("未安装 jinja2，无法在本地渲染模板")
        return _JinjaTemplate(tmpl).render(**data)

    def _tmp_path(self, suffix: str) -> Path:
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        name = f"wmpvp_{int(time.time() * 1000)}_{os.getpid()}.{suffix}"
        return self._tmp_dir / name

    def _schedule_cleanup(self, path: str, delay: int = 120) -> None:
        """发送后再删除本地临时图，避免占用服务器磁盘。"""
        if path not in self._owned_temps:
            return
        if path.startswith("http"):
            return

        async def _cleaner():
            await asyncio.sleep(delay)
            try:
                p = Path(path)
                if p.exists() and p.name.startswith("wmpvp_"):
                    p.unlink()
            except Exception:
                pass
            finally:
                self._owned_temps.discard(path)

        try:
            asyncio.create_task(_cleaner())
        except RuntimeError:
            pass

    def _cleanup_stale_temps(self, max_age: int = 900) -> None:
        now = time.time()
        try:
            for f in self._tmp_dir.glob("wmpvp_*"):
                if now - f.stat().st_mtime > max_age:
                    f.unlink()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 视图数据
    # ------------------------------------------------------------------ #

    @staticmethod
    def _footer_note(m: Dict[str, Any]) -> str:
        """页脚标注真实数据来源，不再写死 wmpvp。"""
        sid = str(m.get("source") or "")
        label = SOURCE_LABELS.get(sid)
        if not label:
            return "数据来源：完美世界电竞"
        if sid == SOURCE_5E:
            return "数据来源：5EPlay 赛事中心"
        if sid == SOURCE_WM:
            return "数据来源：完美世界数据中心"
        return "数据来源：完美世界电竞"

    def _build_view(self, m: Dict[str, Any]) -> Dict[str, Any]:
        status = m.get("status") or STATUS_UPCOMING
        maps = m.get("maps") or []
        players = m.get("players") or []

        id_a = str(m.get("team_a_id") or "").strip()
        id_b = str(m.get("team_b_id") or "").strip()
        distinct = bool(id_a) and bool(id_b) and id_a != id_b
        a_players = [p for p in players if distinct and str(p.get("team_id")) == id_a][:5]
        b_players = [p for p in players if distinct and str(p.get("team_id")) == id_b][:5]
        if not a_players and not b_players and players:
            a_players, b_players = players[:5], players[5:10]

        def _prow(p: Dict[str, Any]) -> Dict[str, Any]:
            rating = _safe_float(p.get("rating"))
            return {
                "nickname": p.get("nickname") or "-",
                "avatar": p.get("avatar") or "",
                "initial": _initial(p.get("nickname") or ""),
                "kd": p.get("kd") or "-",
                "swing": p.get("swing") or "-",
                "adr": p.get("adr") or "-",
                "rating": f"{rating:.2f}" if rating else "-",
                "rating_cls": _rating_cls(rating),
            }

        map_rows = []
        images = m.get("map_images") or {}
        for item in maps:
            idx = _safe_int(item.get("map_index"))
            map_name = item.get("map_name") or ""
            key = map_name.strip().lower()
            map_rows.append({
                "label": f"图{idx + 1} · {map_name}",
                "sa": _safe_int(item.get("score_a")),
                "sb": _safe_int(item.get("score_b")),
                "cls": item.get("status") or "",
                # 本地内置图优先，缺失才回退远程（wiki → 接口 mapUrl → 静态表）
                "img": (self._local_map_image(key)
                        or MAP_IMAGE_WIKI.get(key)
                        or images.get(key)
                        or MAP_IMAGE_FALLBACK.get(key, "")),
            })

        meta_bits = []
        if m.get("stage"):
            meta_bits.append(m["stage"])
        if m.get("start_time"):
            meta_bits.append(_fmt_ts(m["start_time"]))
        if m.get("bo"):
            meta_bits.append(f"BO{m['bo']}")

        sa, sb = _safe_int(m.get("score_a")), _safe_int(m.get("score_b"))
        if status == STATUS_FINISHED:
            hint = "最终比分"
        elif status == STATUS_LIVE:
            hint = "进行中"
        else:
            hint = "尚未开始"

        # -- 支持率与 VRS（二者都可能缺，缺就不渲染对应区块） --------------- #
        sr = m.get("support_rate") or {}
        support = {}
        if sr.get("a") is not None and sr.get("b") is not None:
            support = {
                "a": _safe_int(sr["a"]),
                "b": _safe_int(sr["b"]),
                "total": _safe_int(sr.get("votes_total")),
            }

        rp = m.get("rank_predict") or {}
        vrs_rows = []
        for key in ("a", "b"):
            d = rp.get(key) or {}
            if not d:
                continue
            name = d.get("name") or (m.get("team_a") if key == "a" else m.get("team_b")) or ""
            score = _safe_int(d.get("score"))
            rank = _safe_int(d.get("rank"))
            vrs_rows.append({
                "name": name,
                "initial": _initial(name),
                "score": score,
                "rank": f"#{rank}" if rank else "—",
                "win": _signed(d.get("win_delta")),
                "lose": _signed(d.get("lose_delta")),
                "win_cls": "up" if _safe_int(d.get("win_delta")) > 0 else "flat",
                "lose_cls": "down" if _safe_int(d.get("lose_delta")) < 0 else "flat",
                "is_a": key == "a",
            })

        return {
            "event_name": m.get("event_name") or "CS 赛事",
            "status_class": status,
            "status_text": STATUS_TEXT.get(status, "未开始"),
            "meta_line": " · ".join(meta_bits) or "—",
            "team_a": m.get("team_a") or "TBD",
            "team_b": m.get("team_b") or "TBD",
            "team_a_logo": m.get("team_a_logo") or "",
            "team_b_logo": m.get("team_b_logo") or "",
            "team_a_initial": _initial(m.get("team_a") or ""),
            "team_b_initial": _initial(m.get("team_b") or ""),
            "score_a": sa,
            "score_b": sb,
            "score_a_class": "win" if sa >= sb else "lose",
            "score_b_class": "win" if sb >= sa else "lose",
            "score_hint": hint,
            "empty_hint": ("比赛尚未开始，地图与选手数据将在开赛后同步"
                           if status == STATUS_UPCOMING else "暂无地图与选手数据"),
            "maps": map_rows,
            "a_players": [_prow(p) for p in a_players],
            "b_players": [_prow(p) for p in b_players],
            "support": support,
            "vrs_rows": vrs_rows,
            "comment_count": _safe_int(m.get("comment_count")),
            # 赛前预告专用，普通详情卡留空即可（模板里按空值跳过）
            "preview": False,
            "countdown": "",
            "comments": [],
            "footer_note": self._footer_note(m),
            "generated_at": datetime.now().strftime("%m-%d %H:%M"),
        }

    async def _build_view_with_images(self, match: Dict[str, Any]) -> Dict[str, Any]:
        """构建视图，并把队徽 / 头像 / 地图背景内联成 data URI。

        渲染时页面 origin 是 about:blank 或 file://，浏览器直接请求外链必然失败
        （图床还普遍校验 Referer，缺了就 403）。所以统一在 Python 侧取图再内联：

          * 地图背景：优先读插件内置的 assets/maps/*.webp，缺失才走网络；
          * 队徽 / 头像：按域名补 Referer 抓取，头像用 OSS 参数缩到 96px。
        """
        view = self._build_view(match)

        jobs: List[Tuple[str, int]] = [
            (view.get("team_a_logo") or "", 128),
            (view.get("team_b_logo") or "", 128),
        ]
        for group in ("a_players", "b_players"):
            for p in view.get(group) or []:
                jobs.append((p.get("avatar") or "", 96))
        for row in view.get("maps") or []:
            jobs.append((row.get("img") or "", 420))
        uniq = [(u, w) for u, w in dict.fromkeys(jobs) if u]
        if not uniq:
            return view

        fetched = await asyncio.gather(
            *(self._resolve_image(u, w) for u, w in uniq), return_exceptions=True
        )
        mapping = {u: (r if isinstance(r, str) else "") for (u, _), r in zip(uniq, fetched)}

        view["team_a_logo"] = mapping.get(view.get("team_a_logo") or "", "")
        view["team_b_logo"] = mapping.get(view.get("team_b_logo") or "", "")
        for group in ("a_players", "b_players"):
            for p in view.get(group) or []:
                p["avatar"] = mapping.get(p.get("avatar") or "", "")
        for row in view.get("maps") or []:
            row["img"] = mapping.get(row.get("img") or "", "")
        return view

    @staticmethod
    def _resize_url(url: str, width: int) -> str:
        """按图床能力缩放：OSS 用 x-oss-process，Wikia 用 scale-to-width-down。"""
        if not url:
            return url
        if "static.wikia.nocookie.net" in url:
            return url  # 表里已是 640px 缩略图，无需再处理
        if "x-oss-process" in url:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}x-oss-process=image/resize,m_fixed,w_{width}"

    @staticmethod
    def _sniff_mime(raw: bytes, fallback: str = "") -> str:
        """按魔数判断图片类型：CDN 的 content-type 会写错（webp 报成 image/jpg）。"""
        if raw.startswith(b"\x89PNG"):
            return "image/png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        fallback = (fallback or "").split(";")[0].strip()
        return fallback if fallback.startswith("image/") else "image/png"

    async def _resolve_image(self, value: str, width: int = 96) -> str:
        """把"远程 URL 或本地文件路径"统一解析成 data URI。"""
        if not value:
            return ""
        if value.startswith("http"):
            return await self._image_data_uri(value, width)
        return self._file_data_uri(value)

    def _file_data_uri(self, path: str) -> str:
        try:
            p = Path(path)
            if not p.is_file():
                return ""
            key = f"file|{p}|{p.stat().st_mtime_ns}"
            cached = self._img_cache.get(key)
            if cached:
                return cached[1]
            raw = p.read_bytes()
            if not raw or len(raw) > 512 * 1024:
                return ""
            uri = f"data:{self._sniff_mime(raw, 'image/webp')};base64," + base64.b64encode(raw).decode()
            self._img_cache[key] = (time.time(), uri)
            return uri
        except Exception:
            return ""

    def _local_map_image(self, key: str) -> str:
        """插件内置地图图：assets/maps/<map>.webp（也兼容 jpg/png）。"""
        if not key:
            return ""
        for ext in ("webp", "jpg", "jpeg", "png"):
            p = self._assets_dir / f"{key}.{ext}"
            if p.is_file():
                return str(p)
        return ""

    async def _image_data_uri(self, url: str, width: int = 96) -> str:
        if not url or not url.startswith("http"):
            return ""
        key = f"{width}|{url}"
        now = time.time()
        cached = self._img_cache.get(key)
        if cached and now - cached[0] < 3600:
            return cached[1]
        try:
            status, raw, ctype = await self.client.fetch_binary(self._resize_url(url, width))
            if status != 200 or not raw or len(raw) > 256 * 1024:
                self._img_cache[key] = (now, "")
                return ""
            mime = self._sniff_mime(raw, ctype)
            uri = f"data:{mime};base64," + base64.b64encode(raw).decode()
            self._img_cache[key] = (now, uri)
            return uri
        except Exception:
            self._img_cache[key] = (now, "")
            return ""

    def _build_text_card(self, view: Dict[str, Any]) -> str:
        lines = [
            f"🆚 {view['team_a']} {view['score_a']} : {view['score_b']} {view['team_b']}",
            f"🏆 {view['event_name']} ｜ {view['status_text']}",
            f"🕒 {view['meta_line']}",
        ]
        if view["maps"]:
            lines.append("━━━━━━━━━━━━━━━━")
            for m in view["maps"]:
                lines.append(f"  {_pad(m['label'], 14)}{m['sa']:>2} : {m['sb']:<2}")
        for label, group in (("A 队选手", view["a_players"]), ("B 队选手", view["b_players"])):
            if not group:
                continue
            lines.append(f"━━━━━━━━━━━━━━━━\n{label}")
            for p in group:
                lines.append(
                    f"  {_pad(p['nickname'], 12)} KD {_pad(p['kd'], 8)}"
                    f" ADR {_pad(p['adr'], 6)} RT {p['rating']}"
                )
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("（图片渲染不可用，已回退纯文本）")
        return "\n".join(lines)

    def _api_fail_text(self, exc: ApiError) -> str:
        return (
            f"❌ 赛事接口请求失败\n{exc.brief()}\n\n"
            f"排查：/csgo 测试 API 可查看列表/详情/实时三个接口的状态。\n"
            f"接口本身无需鉴权，若持续失败请检查本机网络或配置中的 api_base。"
        )

    # ------------------------------------------------------------------ #
    # 消息链
    # ------------------------------------------------------------------ #

    def _build_chain(self, text: str, image_path: Optional[str] = None):
        if MessageChain is None:
            raise RuntimeError("当前 AstrBot 版本不支持 MessageChain")
        components: List[Any] = [Plain(text)]
        if image_path:
            components.append(self._make_image(image_path))
        try:
            return MessageChain(chain=components)
        except TypeError:
            return MessageChain(components)

    @staticmethod
    def _make_image(src: str):
        """把渲染结果做成图片组件，兼容 URL / base64 / 本地文件与多种 AstrBot 版本。

        早期版本只试 Image.fromFileSystem，遇到 html_render 返回 URL 时三种构造
        全失败，异常冒泡后上层直接退化成纯文字——这就是「预告只有文字」的根因。
        """
        errors: List[str] = []

        if src.startswith(("http://", "https://")):
            for name, maker in (
                ("fromURL", lambda: Image.fromURL(src)),
                ("url=", lambda: Image(url=src)),
                ("positional", lambda: Image(src)),
            ):
                try:
                    return maker()
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            raise RuntimeError("无法用 URL 构造图片组件：" + "; ".join(errors))

        if src.startswith("base64://"):
            try:
                return Image.fromBase64(src[len("base64://"):])
            except Exception as exc:
                errors.append(f"fromBase64: {exc}")

        path = str(src)
        if not Path(path).exists():
            raise RuntimeError(f"图片文件不存在：{path}")
        for name, maker in (
            ("fromFileSystem", lambda: Image.fromFileSystem(path)),
            ("file=", lambda: Image(file=path)),
            ("positional", lambda: Image(path)),
        ):
            try:
                return maker()
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("无法构造图片组件：" + "; ".join(errors))

    # ------------------------------------------------------------------ #
    # 轮询与推送
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        await asyncio.sleep(8)  # 启动后稍等，避开初始化高峰
        failures = 0

        while True:
            interval = 600
            try:
                matches = await self.client.fetch_match_list(force=True)
                failures = 0
                self._diag["polls"] += 1
                self._diag["last_poll"] = int(time.time())
                self._diag["last_matches"] = len(matches)
                self._diag["last_error"] = ""
                await self._dispatch(matches)
                interval = self._next_interval(matches)
                logger.info(
                    f"[wmpvp_csgo] 轮询 #{self._diag['polls']}：{len(matches)} 场，"
                    f"命中 {self._diag['last_hits']}，累计推送 {self._diag['pushes']} "
                    f"（图片 {self._diag['push_images']} / 纯文字 {self._diag['push_text_only']}），"
                    f"下次 {interval}s")
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                failures += 1
                base = int(self._cfg("interval_idle", 600))
                interval = min(base * (2 ** min(failures, 5)), 1800)
                self._diag["last_error"] = f"ApiError: {exc.brief()}"
                logger.warning(f"[wmpvp_csgo] 轮询失败({failures})，{interval}s 后重试：{exc.brief()}")
            except Exception as exc:
                failures += 1
                interval = min(600 * (2 ** min(failures, 3)), 1800)
                self._diag["last_error"] = f"{type(exc).__name__}: {exc}"
                logger.exception(f"[wmpvp_csgo] 轮询异常：{exc}")

            self._cleanup_stale_temps()
            self.state.prune()
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    def _next_interval(self, matches: List[Dict[str, Any]]) -> int:
        now = datetime.now()
        night_start = int(self._cfg("night_start_hour", 1))
        night_end = int(self._cfg("night_end_hour", 8))
        is_night = now.hour >= night_start and now.hour < night_end
        if is_night:
            return int(self._cfg("interval_night", 1800))

        ts_now = time.time()
        has_live = False
        soon = False
        today = False
        for m in matches:
            status = m.get("status")
            if status == STATUS_LIVE:
                has_live = True
            start = m.get("start_time") or 0
            if status == STATUS_UPCOMING and start:
                if 0 <= start - ts_now <= 1800:
                    soon = True
                if datetime.fromtimestamp(start).date() == now.date():
                    today = True

        if has_live or soon:
            return int(self._cfg("interval_live", 60))
        if today:
            return int(self._cfg("interval_today", 300))
        return int(self._cfg("interval_idle", 600))

    @staticmethod
    def _compute_blocked(matches: List[Dict[str, Any]]) -> Dict[str, bool]:
        """同一赛事里，前面那场还在打 → 后面这场大概率会延后。

        单舞台赛事（PGL/ESL 小组赛）的赛程是串行的：上一场打不完，下一场就一直等。
        这种情况下先别急着推预告，等前一场结束再说，否则用户会收到一个
        「5 分钟后开始」，然后干等一小时。
        """
        by_event: Dict[str, List[Dict[str, Any]]] = {}
        for m in matches:
            name = str(m.get("event_name") or "")
            if name:
                by_event.setdefault(name, []).append(m)

        blocked: Dict[str, bool] = {}
        for rows in by_event.values():
            live = [(_safe_int(m.get("start_time")), str(m.get("match_id")))
                    for m in rows if m.get("status") == STATUS_LIVE]
            if not live:
                continue
            for m in rows:
                if m.get("status") != STATUS_UPCOMING:
                    continue
                st = _safe_int(m.get("start_time"))
                mid = str(m.get("match_id"))
                if not st:
                    continue
                if any(lid != mid and 0 <= st - lst <= BLOCK_SPAN
                       for lst, lid in live):
                    blocked[mid] = True
        return blocked

    async def _dispatch(self, matches: List[Dict[str, Any]]) -> None:
        subs_all = self.subs.all()
        if not subs_all:
            return

        # 1) 先算命中关系：同一场比赛被多个会话订阅时，实时接口只请求一次
        hits: Dict[str, List[str]] = {}
        for umo, snapshot in subs_all.items():
            team_keys: List[str] = []
            for t in snapshot["teams"]:
                if t.get("name"):
                    team_keys.append(_norm(t["name"]))
                for alias in t.get("aliases") or []:
                    if alias:
                        team_keys.append(_norm(alias))
            event_keys: List[str] = []
            for e in snapshot["events"]:
                if e.get("name"):
                    event_keys.append(_norm(e["name"]))
                # LLM/全名匹配到的赛事，也纳入命中范围
                for extra in e.get("matched") or []:
                    if extra:
                        event_keys.append(_norm(extra))
            for m in matches:
                if self._match_hit(m, team_keys, event_keys):
                    hits.setdefault(str(m.get("match_id")), []).append(umo)

        self._diag["last_hits"] = len(hits)
        if not hits:
            return

        # 1.5) 串行赛程：算出哪些比赛正被前一场「压着」
        blocked_map = self._compute_blocked(matches)
        # 本轮「首次见到即反馈」额度：每个会话最多 2 条，避免订阅热门赛事刷屏
        self._hello_left: Dict[str, int] = {u: 2 for u in subs_all}

        # 2) 补全数据：进行中补实时（拿当前第几张图），已结束补详情（拿最后一张图的比分）
        prepared: Dict[str, Dict[str, Any]] = {}
        for m in matches:
            mid = str(m.get("match_id"))
            if mid not in hits:
                continue

            item = m
            status = m.get("status")

            if status == STATUS_LIVE:
                rt = await self.client.fetch_realtime(mid)
                if rt:
                    item = dict(m)
                    self.client.apply_realtime(item, rt)
                    # 订阅晚了会缺图名（显示成「图1」），补一次详情拿图序
                    if self._needs_map_names(item):
                        detail = await self.client.fetch_match_detail(mid)
                        if detail and detail.get("map_slots"):
                            item["map_slots"] = detail["map_slots"]
                            self.client.apply_realtime(item, rt)
                if not item.get("maps"):
                    # 实时接口尚未给出地图（刚开赛/无直播数据）时退回详情
                    detail = await self.client.fetch_match_detail(mid)
                    if detail and detail.get("maps"):
                        item = detail

            elif status == STATUS_FINISHED:
                # 只有还没推送过总结的比赛才拉详情，避免无谓请求
                pending = any(not self.state.get_match(u, mid).get("finished")
                              for u in hits[mid])
                if pending:
                    detail = await self.client.fetch_match_detail(mid)
                    if detail and detail.get("maps"):
                        item = detail

            prepared[mid] = item

        # 3) 逐会话推送（严格只发给命中的会话）
        for umo in subs_all:
            for mid, target_umos in hits.items():
                if umo not in target_umos:
                    continue
                item = prepared.get(mid)
                if not item:
                    continue
                try:
                    await self._process_match(umo, item,
                                              blocked=blocked_map.get(mid, False))
                except Exception as exc:
                    logger.exception(f"[wmpvp_csgo] 会话 {umo} 处理比赛 {mid} 失败：{exc}")
        self.state.save()

    def _group_source(self, umo: str) -> str:
        """群内指定的数据源（覆盖 WebUI 配置）。"""
        groups = self.source_override.data.get("groups") or {}
        sid = str(groups.get(umo) or "")
        return sid if sid in (SOURCE_5E, SOURCE_WM, SOURCE_GW) else ""

    def _client_for(self, umo: str) -> MultiSourceClient:
        """按「群指令 > WebUI 配置 > auto」取实际使用的客户端。

        群内指定时单独建一个锁定单源的客户端（失败直接报错，不静默切换）。
        """
        sid = self._group_source(umo)
        if not sid:
            return self.client
        if sid == self.client.source_id and self.client.locked:
            return self.client
        cached = self._single_clients.get(sid)
        if cached is None:
            src = next((s for s in self.client.sources
                        if getattr(s, "source_id", "") == sid), None)
            if src is None:
                src = self._make_source(sid)
            cached = MultiSourceClient([src], locked=True)
            self._single_clients[sid] = cached
        return cached

    async def _cmd_diag(self, param: str = "") -> str:
        """把「为什么不推送」一次性摊开：轮询、订阅、数据源、渲染、开关。"""
        d = self._diag
        now = int(time.time())
        poll_alive = (self._poll_task is not None and not self._poll_task.done())
        since = now - d["last_poll"] if d["last_poll"] else -1

        lines = ["🔍 推送诊断", "━━━━━━━━━━━━━━━━"]
        lines.append(f"轮询任务：{'运行中 ✅' if poll_alive else '未运行 ❌'}")
        lines.append(f"累计轮询：{d['polls']} 次 ｜ 上次：{since}s 前" if since >= 0
                     else "累计轮询：0 次（还没跑过）")
        lines.append(f"上次取到 {d['last_matches']} 场，订阅命中 {d['last_hits']} 场（上轮轮询口径）")
        if d["last_error"]:
            lines.append(f"⚠️ 上次错误：{d['last_error'][:120]}")

        lines.append("━━━━━━━━━━━━━━━━")
        lines.append(f"累计推送：{d['pushes']} 条（图片 {d['push_images']} / 纯文字 {d['push_text_only']}）")
        if d["last_push"]:
            lines.append(f"最近推送：{now - d['last_push']}s 前 · {d['last_push_note'][:60]}")

        subs_all = self.subs.all()
        total_subs = sum(len(x.get("teams", [])) + len(x.get("events", []))
                         for x in subs_all.values())
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append(f"订阅会话：{len(subs_all)} 个 ｜ 订阅项：{total_subs} 条")
        if not total_subs:
            lines.append("❌ 没有任何订阅，后台不会推送任何比赛！")
        if d["last_hits"] == 0 and total_subs:
            lines.append("❌ 订阅没命中任何比赛，检查队名/赛事名是否与赛程一致")

        # 订阅明细：逐条算命中数，命中 0 时给出最接近的名字，避免用户盲猜
        if total_subs:
            try:
                rows_now = await self.client.fetch_match_list()
            except Exception:
                rows_now = []
            event_pool = sorted({str(m.get("event_name") or "")
                                 for m in rows_now if m.get("event_name")})
            team_pool = sorted({n for m in rows_now
                                for n in (str(m.get("team_a") or ""),
                                          str(m.get("team_b") or "")) if n})
            lines.append("【订阅明细】（实时重算，命中数会随赛程变化）")
            for umo_s, snap in subs_all.items():
                for t in snap.get("teams", []):
                    name = str(t.get("name") or "")
                    k = _norm(name)
                    n = sum(1 for m in rows_now if self._match_hit(m, [k], []))
                    flag = "✅" if n else "❌"
                    lines.append(f"  {flag} 队伍「{name}」→ 命中 {n} 场")
                    if not n and team_pool:
                        near = difflib.get_close_matches(name, team_pool, n=3, cutoff=0.3)
                        if near:
                            lines.append(f"      近似：{'、'.join(near)}")
                for e in snap.get("events", []):
                    name = str(e.get("name") or "")
                    keys = [_norm(name)] + [_norm(x) for x in (e.get("matched") or [])]
                    keys = [k for k in keys if k]
                    n = sum(1 for m in rows_now if self._match_hit(m, [], keys))
                    flag = "✅" if n else "❌"
                    lines.append(f"  {flag} 赛事「{name}」→ 命中 {n} 场")
                    if not n:
                        near = difflib.get_close_matches(name, event_pool, n=3, cutoff=0.25)
                        if near:
                            lines.append(f"      近似赛事：{'、'.join(near)}")
            if not rows_now:
                lines.append("  ⚠️ 当前取不到赛程，无法比对")
            else:
                # 汇总所有订阅键，重算命中比赛，并给出「这轮为什么没推」
                all_team_keys: List[str] = []
                all_event_keys: List[str] = []
                for snap in subs_all.values():
                    for t in snap.get("teams", []):
                        if t.get("name"):
                            all_team_keys.append(_norm(t["name"]))
                        for a in t.get("aliases") or []:
                            if a:
                                all_team_keys.append(_norm(a))
                    for e in snap.get("events", []):
                        if e.get("name"):
                            all_event_keys.append(_norm(e["name"]))
                        for x in e.get("matched") or []:
                            if x:
                                all_event_keys.append(_norm(x))
                hit_rows = [m for m in rows_now
                            if self._match_hit(m, all_team_keys, all_event_keys)]
                lines.append(f"【命中比赛】共 {len(hit_rows)} 场")
                for m in hit_rows[:6]:
                    mid_s = str(m.get("match_id") or "")
                    left = _safe_int(m.get("start_time")) - now
                    st = m.get("status")
                    known = any(self.state.get_match(u, mid_s) for u in subs_all)
                    if not known:
                        why = "首轮建基线，下一轮起推"
                    elif st == STATUS_LIVE:
                        why = "进行中，地图状态变化时推"
                    elif st == STATUS_FINISHED:
                        why = "已结束，赛果已推或本轮推"
                    elif left > self.preview_minutes * 60:
                        why = f"约 {left // 60} 分钟后进预告窗口"
                    elif left >= 0:
                        why = "预告窗口内，本轮应推"
                    else:
                        why = "原定时间已过，等前序结束补推"
                    icon = {"live": "🔴", "finished": "✅", "upcoming": "🕒"}.get(st, "·")
                    lines.append(f"  {icon} {m.get('team_a')} vs {m.get('team_b')}｜{why}")
                lines.append(f"下一轮间隔：{self._next_interval(rows_now)}s")

        lines.append("━━━━━━━━━━━━━━━━")
        lines.append(f"数据源：全局={self.default_source} ｜ 当前={self.client.source_id}")
        lines.append(f"挂载源：{' → '.join(s.source_id for s in self.client.sources)}"
                     f"（{'锁定不回退' if self.client.locked else '自动回退'}）")

        lines.append("━━━━━━━━━━━━━━━━")
        lines.append(f"开关：轮询={self._cfg('poll_enable', True)} "
                     f"预告={self.push_preview} 图开赛={self.push_map_start} "
                     f"图结束={self.push_map_end} 赛果={self.push_match_end}")
        lines.append(f"预告窗口：开赛前 {self.preview_minutes} 分钟")

        # 渲染能力
        hr = callable(getattr(self, "html_render", None)) or \
            callable(getattr(self.context, "html_render", None))
        try:
            import importlib.util
            pw = importlib.util.find_spec("playwright") is not None
        except Exception:
            pw = False
        chrome = self._find_chrome()
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append(f"渲染：html_render={'可用' if hr else '不可用'} ｜ "
                     f"Playwright={'可用' if pw else '未安装'} ｜ "
                     f"Chrome={'找到' if chrome else '未找到'}")
        if not (hr or pw or chrome):
            lines.append("❌ 三级渲染全不可用，所有推送只能是纯文字！")
        lines.append(f"上次渲染：{d.get('render_last', '尚未渲染')[:160]}")

        if (param or "").strip() in ("渲染", "render"):
            try:
                view = self._build_view(self.client.normalize_match(MOCK_MATCH_DATA))
                path = await self._render_html_to_image(self._tmpl, view)
                lines.append(f"试渲染：{'成功 → ' + str(path)[:80] if path else '失败（会退化为纯文字）'}")
            except Exception as exc:
                lines.append(f"试渲染异常：{type(exc).__name__}: {exc}")

        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("用 /csgo 诊断 渲染 可实测一次渲染链路")
        return "\n".join(lines)

    async def _cmd_source(self, umo: str, param: str) -> str:
        param = (param or "").strip().lower()
        current = self._group_source(umo)
        global_sid = self.default_source
        global_text = ("auto（按优先级自动回退）" if global_sid == "auto"
                       else SOURCE_LABELS.get(global_sid, global_sid))
        if not param:
            lines = ["🌐 数据源设置",
                     f"本群指定：{SOURCE_LABELS.get(current, current) if current else '未指定'}",
                     f"全局配置：{global_text}"]
            lines.append("切换：/csgo 数据源 auto|5eplay|wanmei_data|wmpvp")
            return "\n".join(lines)

        if param == "auto":
            groups = self.source_override.data.setdefault("groups", {})
            groups.pop(umo, None)
            self.source_override.save()
            return f"✅ 已恢复跟随全局配置（{global_text}）"

        if param not in SOURCE_ALIASES:
            return "❓ 可选：auto / 5eplay / wanmei_data / wmpvp"
        sid = SOURCE_ALIASES[param]
        self.source_override.data.setdefault("groups", {})[umo] = sid
        self.source_override.save()
        return (f"✅ 本群数据源已设为：{SOURCE_LABELS.get(sid, sid)}\n"
                f"该源不可用时直接报错，不会自动切换到其他源。")

    @staticmethod
    def _match_hit(m: Dict[str, Any], team_keys: List[str], event_keys: List[str]) -> bool:
        if event_keys:
            raw_event = m.get("event_name") or ""
            event_name = _norm(raw_event)
            # 同一赛事在不同数据源命名不同：5E 叫「IEM 北京 2026」，
            # 完美系叫「Intel Extreme Masters BeiJing 2026」。补一条缩写匹配，
            # 否则用户订阅 `iem` 在完美系下会命中 0 场。
            acr = _acronym(raw_event)
            for key in event_keys:
                if not key:
                    continue
                if key in event_name:
                    return True
                # 订阅项可能是「IEM 北京 2026」这种中英混排，归一化后是
                # "iem北京2026"，直接比缩写比不上；把其中的拉丁字母段抽出来再比。
                probes = [key] + re.findall(r"[a-z]{2,8}", key)
                for probe in probes:
                    if acr and (probe == acr or acr.startswith(probe)
                                or probe in acr):
                        return True
        if team_keys:
            ta, tb = _norm(m.get("team_a")), _norm(m.get("team_b"))
            for key in team_keys:
                if not key:
                    continue
                if key == ta or key == tb or key in ta or key in tb:
                    return True
                if ta and ta in key or tb and tb in key:
                    return True
        return False

    @staticmethod
    def _needs_map_names(match: Dict[str, Any]) -> bool:
        """进行中/已结束的图还挂着「图N」占位名 → 需要补真名。"""
        for m in match.get("maps") or []:
            if m.get("status") in (STATUS_LIVE, STATUS_FINISHED):
                if str(m.get("map_name") or "").startswith("图"):
                    return True
        return False

    async def _process_match(self, umo: str, m: Dict[str, Any],
                             blocked: bool = False) -> None:
        match_id = str(m.get("match_id") or "")
        if not match_id:
            return

        record = self.state.get_match(umo, match_id) or {}
        first_sight = not record  # 首次见到该比赛：只建立基线，不推送，避免订阅瞬间刷屏
        map_fps: Dict[str, str] = record.get("maps", {}) or {}
        finished_pushed = bool(record.get("finished"))
        changed = False

        for item in m.get("maps") or []:
            idx = str(item.get("map_index"))
            status = item.get("status")
            fp = f"{match_id}:{idx}:{status}:{item.get('score_a')}:{item.get('score_b')}"
            prev_fp = map_fps.get(idx)

            if prev_fp is None:
                map_fps[idx] = fp
                changed = True
                continue
            if prev_fp == fp:
                continue

            # 用 rsplit 从尾部取：match_id 可能自带冒号（5EPlay 的 "5e:csgo_mc_xxx"），
            # 早期写成 split(":")[2] 会取到 idx 而不是 status，导致地图推送全部失效。
            parts = prev_fp.rsplit(":", 3)
            prev_status = parts[1] if len(parts) == 4 else ""
            map_fps[idx] = fp
            changed = True

            if status == STATUS_LIVE and prev_status == STATUS_UPCOMING and self.push_map_start:
                await self._push_text(umo, self._map_start_text(m, item))
            elif status == STATUS_FINISHED and prev_status == STATUS_LIVE and self.push_map_end:
                await self._push_map_card(umo, m, item)

        if (m.get("status") == STATUS_FINISHED and not finished_pushed
                and self.push_match_end and not first_sight):
            finished_pushed = True
            changed = True
            await self._push_match_summary(umo, m)

        # 赛前预告：开赛前 N 分钟内推一次（谁打谁 + 支持率 + VRS + 热评）
        preview_pushed = bool(record.get("preview"))
        preview_start = _safe_int(record.get("preview_start"))
        start_ts = _safe_int(m.get("start_time"))
        now_ts = int(time.time())
        status_now = m.get("status")

        if self.push_preview and preview_pushed and start_ts:
            # 改期：开赛时间被改走超过 30 分钟，视为重新排期，允许按新时间再推一次
            if (status_now == STATUS_UPCOMING
                    and preview_start and abs(start_ts - preview_start) > RESCHEDULE_RESET):
                logger.info(f"[wmpvp_csgo] 比赛 {match_id} 开赛时间变动 "
                            f"{preview_start} → {start_ts}，重置赛前预告")
                preview_pushed = False
                preview_start = 0
                changed = True

        # 前一场还在打（单舞台串行赛程）就先按住，但上游状态可能卡在 live，
        # 压制必须有上限，否则预告永远发不出去。
        blocked_since = _safe_int(record.get("blocked_since"))
        block_released = bool(record.get("block_released"))
        if blocked and block_released:
            blocked = False          # 已经超时放行过，不再被反复压住
        if blocked:
            if not blocked_since:
                blocked_since = now_ts
                changed = True
            if now_ts - blocked_since > MAX_BLOCK_WAIT:
                logger.info(f"[wmpvp_csgo] 比赛 {match_id} 被前序比赛压制超过 "
                            f"{MAX_BLOCK_WAIT}s，强制放行预告")
                blocked = False
                blocked_since = 0
                block_released = True
                changed = True
        elif blocked_since:
            blocked_since = 0
            changed = True

        if self.push_preview and not preview_pushed and start_ts and not blocked:
            if status_now == STATUS_UPCOMING:
                left = start_ts - now_ts
                if 0 <= left <= self.preview_minutes * 60:
                    preview_pushed = True
                    preview_start = start_ts
                    changed = True
                    await self._push_preview_card(umo, m)
                elif 0 < now_ts - start_ts <= DELAYED_GRACE:
                    # 原定时间已过但还没开打：前面那场拖了，等它结束就补推
                    preview_pushed = True
                    preview_start = start_ts
                    changed = True
                    await self._push_preview_card(umo, m, delayed=True)
            elif (status_now == STATUS_LIVE and not first_sight
                  and 0 <= now_ts - start_ts <= EARLY_START_GRACE):
                # 提前开打：还没等到预告窗口就已经开赛，补一张「已开赛」，
                # 否则这场比赛的阵容/支持率/VRS 会整场都看不到
                preview_pushed = True
                preview_start = start_ts
                changed = True
                await self._push_preview_card(umo, m, started=True)

        # 首次见到就是「进行中 / 已结束」的比赛：给一条即时赛况。
        # 否则这类比赛不会再有地图状态变化，用户订阅后/重启后会全程静默。
        if (first_sight and status_now in (STATUS_LIVE, STATUS_FINISHED)
                and not preview_pushed and not finished_pushed):
            budget = getattr(self, "_hello_left", {}).get(umo, 0)
            if budget > 0:
                self._hello_left[umo] = budget - 1
                changed = True
                if status_now == STATUS_FINISHED:
                    finished_pushed = True   # 避免下一轮再推一次赛果，重复
                await self._push_text(umo, self._hello_text(m))

        if changed or not record:
            self.state.set_match(umo, match_id, {
                "maps": map_fps,
                "finished": finished_pushed,
                "preview": preview_pushed,
                "preview_start": preview_start,
                "blocked_since": blocked_since,
                "block_released": block_released,
                "updated_at": int(time.time()),
            })

    def _hello_text(self, m: Dict[str, Any]) -> str:
        """订阅/重启后首次见到「进行中或已结束」比赛时的即时赛况。"""
        sa, sb = _safe_int(m.get("score_a")), _safe_int(m.get("score_b"))
        state = "🔴 正在进行" if m.get("status") == STATUS_LIVE else "🏁 已结束"
        lines = ["📌 你订阅的比赛",
                 f"{m.get('team_a')} {sa} : {sb} {m.get('team_b')}", state]
        if m.get("event_name"):
            lines.append(str(m["event_name"]))
        for item in (m.get("maps") or [])[-1:]:
            lines.append(f"  图{_safe_int(item.get('map_index')) + 1} · "
                         f"{item.get('map_name') or ''}  "
                         f"{item.get('score_a')}:{item.get('score_b')}")
        lines.append("后续动态会继续推送到本会话。")
        return "\n".join(lines)

    def _map_start_text(self, m: Dict[str, Any], item: Dict[str, Any]) -> str:
        idx = _safe_int(item.get("map_index"))
        lines = [
            f"🔴 图{idx + 1} 开赛 · {item.get('map_name')}",
            f"{m.get('team_a')} vs {m.get('team_b')}",
        ]
        if m.get("event_name"):
            lines.append(m["event_name"])
        lines.append(f"当前大比分 {m.get('score_a')} : {m.get('score_b')}")
        return "\n".join(lines)

    def _map_end_text(self, m: Dict[str, Any], item: Dict[str, Any]) -> str:
        idx = _safe_int(item.get("map_index"))
        sa, sb = _safe_int(item.get("score_a")), _safe_int(item.get("score_b"))
        winner = m.get("team_a") if sa > sb else (m.get("team_b") if sb > sa else "平局")
        return (
            f"✅ 图{idx + 1} 结束 · {item.get('map_name')}\n"
            f"{m.get('team_a')} {sa} : {sb} {m.get('team_b')}\n"
            f"本图胜者：{winner}\n"
            f"大比分 {m.get('score_a')} : {m.get('score_b')}"
        )

    async def _push_map_card(self, umo: str, m: Dict[str, Any], item: Dict[str, Any]) -> None:
        """地图结束：优先推送数据卡，失败退回文本。"""
        shown = dict(item)
        try:
            # 强制绕过缓存：地图刚结束，需要最新的逐图比分
            detail = await self.client.fetch_match_detail(str(m.get("match_id")), force=True)
        except Exception:
            detail = None

        if detail:
            # 实时接口给不出单图回合分，用详情里的同名图补上真实比分
            for x in detail.get("maps") or []:
                if _safe_int(x.get("map_index")) == _safe_int(item.get("map_index")):
                    if _safe_int(x.get("score_a")) or _safe_int(x.get("score_b")):
                        shown["score_a"] = _safe_int(x.get("score_a"))
                        shown["score_b"] = _safe_int(x.get("score_b"))
                    if x.get("map_name"):
                        shown["map_name"] = x["map_name"]
                    break
            single = dict(detail)
        else:
            single = dict(m)

        # 只展示刚结束的这张图，选手数据也换成这一张图的
        single["maps"] = [shown]
        per_map = (detail or {}).get("map_players") or m.get("map_players") or {}
        rows = per_map.get(_safe_int(item.get("map_index")))
        if rows:
            single["players"] = rows
        view = await self._build_view_with_images(single)

        path = await self._render_html_to_image(self._tmpl, view)
        if path:
            try:
                chain = self._build_chain(self._map_end_text(m, shown), path)
                await self.context.send_message(umo, chain)
                self._schedule_cleanup(path)
                self._record_push(True, f"地图卡图片 {Path(path).name}")
                return
            except Exception as exc:
                logger.warning(f"[wmpvp_csgo] 地图卡片推送失败，改发文本：{exc}")
        await self._push_text(umo, self._map_end_text(m, shown))

    async def _push_preview_card(self, umo: str, m: Dict[str, Any],
                                 started: bool = False,
                                 delayed: bool = False) -> None:
        """赛前预告：一张卡交代「谁打谁 + 支持率 + VRS + 热评」。

        started=True 表示比赛提前开打、预告没赶上，改推「已开赛」版本。
        """
        single = dict(m)
        # 预告阶段还没有地图数据，列表里的 maps 往往是占位，清掉更干净
        single["maps"] = []
        try:
            detail = await self.client.fetch_match_detail(str(m.get("match_id")), force=True)
            if detail:
                single.update({k: v for k, v in detail.items() if k != "maps"})
                single["maps"] = []
        except Exception:
            pass

        await self._attach_extras(single)
        comments: List[Dict[str, Any]] = []
        mid = str(single.get("match_id") or "")
        gw = self._gw_source()
        if gw is not None and mid and not mid.startswith(f"{SOURCE_5E}:"):
            try:
                comments = await gw.fetch_comments(mid, 3)
            except Exception:
                comments = []
        elif gw is not None and mid:
            resolved = await self._resolve_wmpvp_id(single, gw)
            if resolved:
                try:
                    comments = await gw.fetch_comments(resolved, 3)
                except Exception:
                    comments = []

        view = await self._build_view_with_images(single)
        view["preview"] = True
        view["comments"] = comments
        if started:
            view["countdown"] = "已开赛"
        elif delayed:
            view["countdown"] = "前一场刚结束，即将开始"
        else:
            view["countdown"] = self._countdown_text(_safe_int(single.get("start_time")))

        path = await self._render_html_to_image(self._tmpl, view)
        text = self._preview_text(single, view, started, delayed)
        if path:
            try:
                await self.context.send_message(umo, self._build_chain(text, path))
                self._schedule_cleanup(path)
                self._record_push(True, f"赛前预告图片 {Path(path).name}")
                return
            except Exception as exc:
                logger.warning(f"[wmpvp_csgo] 赛前预告带图发送失败（path={path!r} "
                               f"exists={Path(path).exists() if not path.startswith('http') else 'URL'}）：{exc}")
        else:
            logger.warning("[wmpvp_csgo] 赛前预告渲染失败，只能发文字")
        self._diag["last_push_note"] = "赛前预告降级为纯文字"
        await self._push_text(umo, text)

    @staticmethod
    def _countdown_text(start_ts: int) -> str:
        if not start_ts:
            return ""
        left = start_ts - int(time.time())
        if left <= 0:
            return "即将开始"
        if left < 60:
            return "不到 1 分钟后开始"
        return f"{left // 60} 分钟后开始"

    def _preview_text(self, m: Dict[str, Any], view: Dict[str, Any],
                      started: bool = False, delayed: bool = False) -> str:
        if started:
            head = "🔴 已开赛（原定时间有变）"
        elif delayed:
            head = "🔔 赛前预告（已延后）"
        else:
            head = "🔔 赛前预告"
        lines = [
            head,
            f"{m.get('team_a')} vs {m.get('team_b')}",
        ]
        if view.get("countdown"):
            lines.append(f"⏰ {view['countdown']}")
        meta = [x for x in (m.get("event_name"), m.get("stage"),
                            f"BO{m['bo']}" if m.get("bo") else "") if x]
        if meta:
            lines.append(" · ".join(meta))
        sr = view.get("support") or {}
        if sr:
            lines.append(f"👍 支持率 {sr['a']}% : {sr['b']}%")
        return "\n".join(lines)

    def _gw_source(self) -> Any:
        for s in getattr(self.client, "sources", []):
            if getattr(s, "source_id", "") == SOURCE_GW:
                return s
        return None

    async def _push_match_summary(self, umo: str, m: Dict[str, Any]) -> None:
        """整场结束：优先推「总数据卡」（全部地图 + 5v5 + 支持率），失败回退文字。"""
        sa, sb = _safe_int(m.get("score_a")), _safe_int(m.get("score_b"))
        winner = m.get("team_a") if sa > sb else (m.get("team_b") if sb > sa else "平局")
        lines = [
            "🏁 比赛结束",
            f"{m.get('team_a')} {sa} : {sb} {m.get('team_b')}",
            f"胜者：{winner}",
        ]
        if m.get("event_name"):
            lines.append(m["event_name"])
        if m.get("bo"):
            lines.append(f"BO{m['bo']}")
        text = "\n".join(lines)

        # _dispatch 对已结束比赛通常已经补过详情；没有就再拉一次拿逐图与选手
        shown = m
        if not (m.get("players") or m.get("maps")):
            try:
                detail = await self.client.fetch_match_detail(
                    str(m.get("match_id") or ""), force=True)
                if detail:
                    shown = detail
            except Exception:
                pass
        try:
            await self._attach_extras(shown)
            view = await self._build_view_with_images(shown)
            path = await self._render_html_to_image(self._tmpl, view)
        except Exception as exc:
            logger.warning(f"[wmpvp_csgo] 赛果卡渲染异常：{exc}")
            path = None

        if path:
            try:
                await self.context.send_message(umo, self._build_chain(text, path))
                self._schedule_cleanup(path)
                self._record_push(True, f"赛果总数据卡 {Path(path).name}")
                return
            except Exception as exc:
                logger.warning(f"[wmpvp_csgo] 赛果卡发送失败（path={path!r}）：{exc}")
        await self._push_text(umo, text)

    def _record_push(self, image: bool, note: str = "") -> None:
        d = self._diag
        d["pushes"] += 1
        d["push_images" if image else "push_text_only"] += 1
        d["last_push"] = int(time.time())
        d["last_push_note"] = note

    async def _push_text(self, umo: str, text: str) -> None:
        try:
            await self.context.send_message(umo, self._build_chain(text))
            self._record_push(False, "纯文字")
        except Exception as exc:
            logger.warning(f"[wmpvp_csgo] 推送到 {umo} 失败：{exc}")
