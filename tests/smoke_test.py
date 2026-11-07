# -*- coding: utf-8 -*-
"""
离线冒烟测试（无需安装 AstrBot / 无需联网也能跑大部分用例）。

用法：
    cd astrbot_plugin_wmpvp_csgo
    python tests/smoke_test.py

覆盖：
    模板结构、指令分发、三级渲染降级、主动推送路由、订阅多群隔离、
    地图级状态机（开赛/结束/整场）、轮询间隔、真实接口探测与冷却。

说明：tests/stubs 下是 astrbot 的最小桩实现，仅在本脚本运行时注入 sys.path，
不会影响 AstrBot 真实运行。
"""

import asyncio
import re
import sys
import tempfile
import time
from pathlib import Path

STUBS = Path(__file__).resolve().parent / "stubs"
PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUBS))
sys.path.insert(0, str(PLUGIN))

import main as plugin_mod  # noqa: E402
from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.star import Context  # noqa: E402

# 每次测试用全新数据目录，避免上次运行的 subs.json / state.json 影响断言
_DATA = Path(tempfile.mkdtemp(prefix="wmpvp_data_"))
plugin_mod._data_dir = lambda: _DATA

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    suffix = f"  <- {detail}" if detail and not cond else ""
    print(("  PASS " if cond else "  FAIL ") + name + suffix)


async def _async_none():
    return None


async def run(plugin, cmd, umo="qq:GroupMessage:111"):
    event = AstrMessageEvent(cmd, umo)
    return [r async for r in plugin.csgo(event)]


# --------------------------------------------------------------------------- #
# 迷你 Jinja 子集渲染器：校验模板结构与变量名（不参与生产）
# --------------------------------------------------------------------------- #
class Dot(dict):
    def __getattr__(self, k):
        if k not in self:
            raise AttributeError(f"模板引用了不存在的变量: {k}")
        return self[k]


def wrap(obj):
    if isinstance(obj, dict):
        return Dot({k: wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [wrap(x) for x in obj]
    return obj


TOKEN = re.compile(r"(\{\{.*?\}\}|\{%.*?%\})", re.S)


def build(tokens, i=0, stop=None):
    nodes = []
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("{%"):
            tag = t[2:-2].strip()
            name = tag.split()[0] if tag else ""
            if stop and name in stop:
                return nodes, i
            if name == "if":
                body, i = build(tokens, i + 1, {"else", "endif"})
                else_body = []
                if tokens[i][2:-2].strip() == "else":
                    else_body, i = build(tokens, i + 1, {"endif"})
                nodes.append(("if", tag[3:].strip(), body, else_body))
                i += 1
            elif name == "for":
                m = re.match(r"for\s+(\w+)\s+in\s+(.+)", tag)
                body, i = build(tokens, i + 1, {"endfor"})
                nodes.append(("for", m.group(1), m.group(2).strip(), body))
                i += 1
            else:
                i += 1
        elif t.startswith("{{"):
            nodes.append(("expr", t[2:-2].strip()))
            i += 1
        else:
            nodes.append(("text", t))
            i += 1
    return nodes, i


def render(nodes, ns):
    buf = []
    for node in nodes:
        if node[0] == "text":
            buf.append(node[1])
        elif node[0] == "expr":
            val = eval(node[1], {"__builtins__": {}}, ns)  # noqa: S307
            buf.append("" if val is None else str(val))
        elif node[0] == "if":
            _, cond, body, else_body = node
            branch = body if eval(cond, {"__builtins__": {}}, ns) else else_body
            buf.append(render(branch, ns))
        elif node[0] == "for":
            _, var, iterable, body = node
            for item in eval(iterable, {"__builtins__": {}}, ns):
                scope = dict(ns)
                scope[var] = item
                buf.append(render(body, scope))
    return "".join(buf)


def render_template(tmpl, view):
    nodes, _ = build(TOKEN.split(tmpl))
    return render(nodes, wrap(view))


# --------------------------------------------------------------------------- #
async def main():
    print("\n=== 1. 模板结构校验 ===")
    ctx = Context()
    plugin = plugin_mod.WmpvpCsgoPlugin(ctx, {"demo_mode": True, "poll_enable": False})
    view = plugin._build_view(plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA))
    html = render_template(plugin._tmpl, view)
    check("模板渲染无残留标记", "{{" not in html and "{%" not in html)
    check("模板含双方队名", "FaZe Clan" in html and "Vitality" in html)
    player_rows = html.count('class="pname"')
    check("模板含 10 名选手", player_rows == 10, f"实际 {player_rows}")
    map_rows = html.count('class="map-row')
    check("模板含 3 张地图", map_rows == 3, f"实际 {map_rows}")
    check("模板含比分", ">2<" in html and ">1<" in html)

    print("\n=== 2. 帮助与未知指令 ===")
    out = await run(plugin, "/csgo")
    check("无参返回帮助", out and "完美世界电竞" in out[0].payload)
    out = await run(plugin, "/csgo 不存在的指令")
    check("未知指令提示", "未知指令" in out[0].payload)

    print("\n=== 3. 测试 渲染（三级降级 → 文本） ===")
    out = await run(plugin, "/csgo 测试 渲染")
    check("渲染失败时回退纯文本",
          [r.kind for r in out] == ["plain"] and "测试失败" in out[0].payload)
    check("文本卡片含选手数据", "broky" in out[0].payload and "ZywOo" in out[0].payload)

    print("\n=== 4. 测试 推送 ===")
    ctx.sent.clear()
    out = await run(plugin, "/csgo 测试 推送", "qq:GroupMessage:222")
    check("推送成功提示", "已发起独立推送" in out[0].payload)
    check("推送路由到当前会话",
          len(ctx.sent) == 1 and ctx.sent[0][0] == "qq:GroupMessage:222")
    check("推送内容含会话标识", "qq:GroupMessage:222" in ctx.sent[0][1].chain[0].text)

    print("\n=== 5. 订阅隔离 ===")
    await run(plugin, "/csgo 队伍订阅 faze", "qq:GroupMessage:111")
    await run(plugin, "/csgo 赛事订阅 IEM 成都", "qq:GroupMessage:111")
    a = (await run(plugin, "/csgo 我的订阅", "qq:GroupMessage:111"))[0].payload
    b = (await run(plugin, "/csgo 我的订阅", "qq:GroupMessage:222"))[0].payload
    check("A 群看到队伍订阅", "FaZe Clan" in a, a[:60])
    check("A 群看到赛事订阅", "IEM" in a, a[:60])
    check("B 群订阅为空（隔离）", "还没有任何订阅" in b, b[:60])
    out = await run(plugin, "/csgo 队伍订阅 Faze", "qq:GroupMessage:111")
    check("重复订阅被拦截", "已订阅过" in out[0].payload)
    out = await run(plugin, "/csgo 队伍取消 faze", "qq:GroupMessage:111")
    check("取消订阅成功", "已取消" in out[0].payload)

    print("\n=== 6. 查询类指令（demo 数据） ===")
    out = await run(plugin, "/csgo 赛程")
    check("赛程列表含比分", "FaZe Clan 2 : 1 Vitality" in out[0].payload, out[0].payload[:80])
    out = await run(plugin, "/csgo 比分 faze")
    check("比分查询命中", "相关比赛" in out[0].payload)
    out = await run(plugin, "/csgo 详情 900001")
    check("详情回退文本卡", "选手数据" in out[0].payload or "KD" in out[0].payload)
    out = await run(plugin, "/csgo 详情 123456789")
    check("未知比赛ID提示", "未找到比赛" in out[0].payload)

    print("\n=== 7. 状态机与轮询间隔 ===")
    mock = plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA)
    check("命中队伍订阅", plugin._match_hit(mock, ["fazeclan"], []))
    check("命中赛事订阅", plugin._match_hit(mock, [], ["iem成都2026"]))
    check("未命中订阅", not plugin._match_hit(mock, ["navi"], []))
    # 固定夜间窗口，避免断言随运行时刻漂移
    plugin.config["night_start_hour"] = 0
    plugin.config["night_end_hour"] = 0
    check("已结束比赛 → 空闲间隔", plugin._next_interval([mock]) == 600)
    check("进行中 → 60s", plugin._next_interval([dict(mock, status="live")]) == 60)
    plugin.config["night_end_hour"] = 24
    check("夜间 → 1800s", plugin._next_interval([mock]) == 1800)
    plugin.config["night_start_hour"] = 1
    plugin.config["night_end_hour"] = 8

    print("\n=== 8. 地图级推送（模拟 upcoming→live→finished） ===")
    ctx.sent.clear()
    umo = "qq:GroupMessage:333"
    plugin.subs.add_team(umo, "FaZe Clan")

    def to_umo():
        return [c for c in ctx.sent if c[0] == umo]

    m1 = plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA)
    m1["status"] = "live"
    m1["maps"] = [{"map_index": 0, "map_name": "Mirage", "score_a": 0,
                   "score_b": 0, "status": "upcoming"}]
    await plugin._dispatch([m1])
    # 首轮会发一条「首次见到进行中比赛」的赛况反馈（不是地图开赛）
    check("首轮只发赛况、不误报开赛",
          len(to_umo()) == 1 and "你订阅的比赛" in to_umo()[0][1].chain[0].text,
          str([c[1].chain[0].text[:20] for c in to_umo()]))

    m2 = plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA)
    m2["status"] = "live"
    m2["maps"] = [{"map_index": 0, "map_name": "Mirage", "score_a": 3,
                   "score_b": 2, "status": "live"}]
    await plugin._dispatch([m2])
    check("图1开赛推送", len(to_umo()) == 2 and "图1 开赛" in to_umo()[1][1].chain[0].text)

    m3 = plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA)
    m3["status"] = "live"
    m3["maps"] = [{"map_index": 0, "map_name": "Mirage", "score_a": 13,
                   "score_b": 9, "status": "finished"}]
    await plugin._dispatch([m3])
    check("图1结束推送", len(to_umo()) == 3 and "图1 结束" in to_umo()[2][1].chain[0].text)

    await plugin._dispatch([m3])
    check("重复状态不重复推送", len(to_umo()) == 3)

    m4 = plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA)
    await plugin._dispatch([m4])
    check("整场结束推送", len(to_umo()) == 4 and "比赛结束" in to_umo()[3][1].chain[0].text)

    await plugin._dispatch([m4])
    check("整场结束不重复推送", len(to_umo()) == 4)

    print("\n=== 9. 列表无地图时，结束比赛补详情（回归） ===")
    umo2 = "qq:GroupMessage:444"
    plugin.subs.add_team(umo2, "FaZe Clan")
    list_only = {k: v for k, v in plugin_mod.MOCK_MATCH_DATA.items() if k != "statsDTOList"}
    bare = plugin.client.normalize_match(list_only)
    check("列表项确实没有地图", bare["maps"] == [], str(bare["maps"]))

    # 上一轮状态：图1已结束、图2进行中
    plugin.state.set_match(umo2, "900001", {
        "maps": {"0": "900001:0:finished:13:8", "1": "900001:1:live:3:2"},
        "finished": False, "updated_at": 0,
    })

    async def _fake_detail(match_id, force=False):
        return plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA)

    original_detail = plugin.client.fetch_match_detail
    plugin.client.fetch_match_detail = _fake_detail
    ctx.sent.clear()
    try:
        await plugin._dispatch([dict(bare, status="finished")])
    finally:
        plugin.client.fetch_match_detail = original_detail

    msgs = [c[1].chain[0].text for c in ctx.sent if c[0] == umo2]
    check("补详情后仍能推出图2结束", any("图2 结束" in x for x in msgs), str(msgs)[:150])
    check("同时推出整场结束", any("比赛结束" in x for x in msgs), str(msgs)[:150])

    print("\n=== 10. 真实接口探测（/csgo 测试 API） ===")
    live_plugin = plugin_mod.WmpvpCsgoPlugin(Context(), {"poll_enable": False})
    out = await run(live_plugin, "/csgo 测试 API")
    text = out[0].payload
    print(text)
    check("探测覆盖列表/详情/实时", "赛事列表" in text and "比赛详情" in text)
    check("三个数据源都被探测", text.count("·") >= 3, str(text.count("·")))
    check("列表接口可用", "✅" in text and "赛事列表" in text, text[:120])
    # 探测三个源可能耗时超过冷却窗口，这里直接验证冷却机制本身
    live_plugin._test_api_last["qq:GroupMessage:111"] = time.time()
    out2 = await run(live_plugin, "/csgo 测试 API")
    check("冷却生效", "冷却中" in out2[0].payload, out2[0].payload[:80])

    print("\n=== 11. 真实数据链路（列表 → 详情 → 视图） ===")
    out = await run(live_plugin, "/csgo 赛程")
    schedule = out[0].payload
    print(schedule[:600])
    check("赛程含真实赛事", "场" in schedule and "━━" in schedule)
    check("赛程含比赛ID", "ID:" in schedule)

    today_matches = await live_plugin.client.fetch_match_list()
    check("列表解析出比赛", len(today_matches) > 0, f"{len(today_matches)} 场")
    if today_matches:
        first = today_matches[0]
        check("队伍名非空", bool(first["team_a"]) and bool(first["team_b"]),
              f"{first['team_a']} vs {first['team_b']}")
        check("状态已归一化", first["status"] in ("live", "upcoming", "finished"), first["status"])
        check("开始时间是秒级", first["start_time"] > 1_600_000_000, str(first["start_time"]))

    # 详情：优先找已结束的比赛（有地图与选手数据）
    yday = plugin_mod.date.today() - plugin_mod.timedelta(days=1)
    try:
        past = await live_plugin.client.fetch_match_list(yday)
    except Exception as exc:
        past = []
        print(f"  拉取昨日赛程失败：{exc}")
    finished = [m for m in past if m["status"] == "finished"]
    if finished:
        mid = finished[0]["match_id"]
        detail = await live_plugin.client.fetch_match_detail(mid)
        check("详情返回地图", bool(detail["maps"]), f"{len(detail['maps'])} 张图")
        check("详情返回 10 名选手", len(detail["players"]) == 10, f"{len(detail['players'])} 人")
        view = await live_plugin._build_view_with_images(detail)
        check("视图分组：两队都有人",
              len(view["a_players"]) > 0 and len(view["b_players"]) > 0,
              f"{len(view['a_players'])}/{len(view['b_players'])}")
        check("视图分组：人数不超过 5",
              len(view["a_players"]) <= 5 and len(view["b_players"]) <= 5,
              f"{len(view['a_players'])}/{len(view['b_players'])}")
        check("视图分组：绝大多数选手都归了队",
              len(view["a_players"]) + len(view["b_players"]) >= 8,
              f'{len(view["a_players"])}+{len(view["b_players"])} vs {len(detail["players"])}')
        check("队徽已内联为 data URI", view["team_a_logo"].startswith("data:image/"),
              view["team_a_logo"][:40])
        with_bg = [r for r in view["maps"] if r["img"].startswith("data:image/")]
        check("地图行带背景图", len(with_bg) >= 1, f"{len(with_bg)}/{len(view['maps'])} 张有图")
        print("  样例：", f"{detail['team_a']} {detail['score_a']}:{detail['score_b']} {detail['team_b']}")
        print("  地图：", [(x["map_name"], x["score_a"], x["score_b"]) for x in detail["maps"]])
    else:
        print("  昨日无已结束比赛，跳过详情断言")

    # 测试渲染的样本抽取：应优先命中真实已结束比赛
    sample, source = await live_plugin._pick_sample_match()
    check("测试渲染抽到真实比赛", "真实" in source and bool(sample.get("maps")), source)

    print("\n=== 12. 进行中比赛：图1打完是否推图1 ===")
    umo4 = "qq:GroupMessage:666"
    ctx.sent.clear()

    live_base = plugin.client.normalize_match(
        {k: v for k, v in plugin_mod.MOCK_MATCH_DATA.items() if k != "statsDTOList"})
    live_base["status"] = "live"
    live_base["bo"] = 3
    live_base["maps"] = []

    def _rt(box, zh, mid):
        return {"boxNum": box, "finished": False, "mapName": zh, "mapId": mid,
                "team1PlayerList": [], "team2PlayerList": []}

    stage1 = dict(live_base)
    plugin.client.apply_realtime(stage1, _rt(1, "核子危机", 5))
    check("图1开打状态正确", stage1["maps"][0]["status"] == "live"
          and stage1["maps"][0]["map_name"] == "Nuke", str(stage1["maps"][:1]))
    await plugin._process_match(umo4, stage1)          # 首见只建基线
    check("图1开打不误推", len([c for c in ctx.sent if c[0] == umo4]) == 0)

    stage2 = dict(live_base)
    stage2["maps"] = stage1["maps"]
    plugin.client.apply_realtime(stage2, _rt(2, "炼狱小镇", 4))
    check("图2接手且图1转结束", stage2["maps"][0]["status"] == "finished"
          and stage2["maps"][1]["map_name"] == "Inferno", str(stage2["maps"][:2]))

    async def _fake_detail2(match_id, force=False):
        d = plugin.client.normalize_match(plugin_mod.MOCK_MATCH_DATA)
        d["maps"] = [{"map_index": 0, "map_name": "Nuke", "score_a": 13,
                      "score_b": 9, "status": "finished"}]
        return d

    orig2 = plugin.client.fetch_match_detail
    plugin.client.fetch_match_detail = _fake_detail2
    try:
        await plugin._process_match(umo4, stage2)
    finally:
        plugin.client.fetch_match_detail = orig2

    msgs = [c[1].chain[0].text for c in ctx.sent if c[0] == umo4]
    check("图1打完推送图1", any("图1 结束" in x for x in msgs), str(msgs)[:150])
    check("推送带真实回合比分", any("13 : 9" in x for x in msgs), str(msgs)[:150])

    print("\n=== 13. 订阅晚了：占位图名用 veto 图序还原 ===")
    raw_veto = dict(plugin_mod.MOCK_MATCH_DATA)
    raw_veto["singleMatchDataDTOS"] = [
        {"boxNum": 1, "mapId": 15},   # Anubis
        {"boxNum": 2, "mapId": 5},    # Nuke
        {"boxNum": 3, "mapId": 4},    # Inferno
    ]
    veto_match = plugin.client.normalize_match(raw_veto)
    check("从 singleMatchDataDTOS 还原图序",
          veto_match["map_slots"] == {1: "Anubis", 2: "Nuke", 3: "Inferno"},
          str(veto_match["map_slots"]))

    late = dict(live_base)
    late["maps"] = []
    late["map_slots"] = veto_match["map_slots"]
    plugin.client.apply_realtime(late, _rt(3, "炼狱小镇", 4))
    check("图1/图2 用图序补出真名",
          late["maps"][0]["map_name"] == "Anubis" and late["maps"][1]["map_name"] == "Nuke",
          str([(x["map_name"], x["status"]) for x in late["maps"]]))
    check("当前图用实时真名", late["maps"][2]["map_name"] == "Inferno")
    check("缺名时会被识别", plugin._needs_map_names(dict(live_base, maps=[
        {"map_index": 0, "map_name": "图1", "status": "finished"}])))

    print("\n=== 14. 多数据源：优先级与回退 ===")
    ms_plugin = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False,
                    "data_source_priority": "5eplay,wanmei_data,wmpvp"})
    check("优先级解析为 5e→wm→gw",
          [s.source_id for s in ms_plugin.client.sources] == ["5e", "wm", "gw"],
          str([s.source_id for s in ms_plugin.client.sources]))

    alias_plugin = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False, "data_source_priority": "wmpvp"})
    check("别名/单源生效且自动补齐",
          [s.source_id for s in alias_plugin.client.sources][0] == "gw",
          str([s.source_id for s in alias_plugin.client.sources]))

    # 用桩把前两个源打成永久失败，验证回退到第三个
    rows = await ms_plugin.client.fetch_match_list()
    check("三源时能拿到比赛", len(rows) > 0, f"{len(rows)} 场")

    saved = list(ms_plugin.client.sources)
    calls = []
    for src in saved[:2]:
        async def _fail(*a, _src=src, **kw):
            calls.append(_src.source_id)
            raise plugin_mod.ApiError(f"{_src.source_id} 模拟故障")
        src.fetch_match_list = _fail
    try:
        ms_plugin.client.active = saved[0]
        rows2 = await ms_plugin.client.fetch_match_list(force=True)
    finally:
        for i, src in enumerate(saved[:2]):
            src.fetch_match_list = saved[i].__class__.fetch_match_list
    check("前两源失败后回退到第三个", len(rows2) > 0 and ms_plugin.client.active is saved[2],
          f"active={ms_plugin.client.active.source_id}")
    check("回退过程确实试过前两个", calls[:2] == [saved[0].source_id, saved[1].source_id],
          str(calls[:2]))

    # 全部失败要抛出可读错误
    for src in saved:
        src.fetch_match_list = _fail_all = (lambda s=src: (lambda *a, **kw: (_ for _ in ()).throw(
            plugin_mod.ApiError(f"{s.source_id} 全挂"))))()
    try:
        await ms_plugin.client.fetch_match_list(force=True)
        raised = False
    except plugin_mod.ApiError as exc:
        raised = "所有数据源均失败" in str(exc.brief())
    finally:
        for i, src in enumerate(saved):
            src.fetch_match_list = saved[i].__class__.fetch_match_list
    check("全部失败给出可读错误", raised)

    # 5EPlay 比赛 ID 带前缀且能路由回 5E 源
    # 直接用归一化结果验证 5E ID 格式，不依赖当日赛程里恰好有 5E 比赛
    fivee_src = plugin_mod.FivePlayClient()
    fivee_norm = fivee_src.normalize_match({"mc_info": {"id": "csgo_mc_1"}})
    check("5E 比赛 ID 带源前缀",
          str(fivee_norm.get("match_id", "")).startswith("5e:"),
          str(fivee_norm.get("match_id")))
    routed = ms_plugin.client._route(fivee_norm["match_id"])
    check("前缀 ID 路由到 5E 源",
          bool(routed) and routed[0].source_id == "5e",
          str([r.source_id for r in routed]))

    print("\n=== 15. 支持率 / VRS / 评论 ===")
    ex_plugin = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False, "data_source_priority": "wmpvp"})
    gw_src = [x for x in ex_plugin.client.sources if x.source_id == "gw"][0]

    # 支持率：按队伍 ID 取值，且两边加起来正好 100
    raw_sup = dict(plugin_mod.MOCK_MATCH_DATA)
    raw_sup["teamSupportRateMap"] = {"1": 67, "2": 33}
    sup = ex_plugin.client.normalize_match(raw_sup)["support_rate"]
    check("支持率解析正确", sup.get("a") == 67 and sup.get("b") == 33, str(sup))
    check("支持率合计为 100", sup.get("a", 0) + sup.get("b", 0) == 100)

    # 缺一侧数据时不展示，避免错配
    raw_bad = dict(plugin_mod.MOCK_MATCH_DATA)
    raw_bad["teamSupportRateMap"] = {"1": 67}
    check("支持率缺一侧则不展示",
          ex_plugin.client.normalize_match(raw_bad)["support_rate"] == {})

    # VRS：四个分支各取真实值，不做正负对称假设
    vrs_rows = await gw_src.fetch_rank_predict("2399984")
    check("VRS 拿到原始数据", bool(vrs_rows), str(type(vrs_rows)))
    if vrs_rows:
        check("VRS 含初始分与排名",
              vrs_rows["a"]["score"] > 0 and vrs_rows["a"]["rank"] > 0,
              str(vrs_rows["a"]))

    # 已结束的比赛不展示 VRS 预测（结果已知）
    fin_detail = await ex_plugin.client.fetch_match_detail("2399984")
    await ex_plugin._attach_extras(fin_detail)
    check("已结束不展示 VRS", not fin_detail.get("rank_predict"))
    check("已结束仍显示支持率", bool(fin_detail.get("support_rate")))

    # 评论：数量与列表都能取到
    total = await gw_src.fetch_comment_count("2399984")
    check("评论数可获取", total > 0, str(total))
    clist = await gw_src.fetch_comments("2399984", 3)
    check("评论列表非空", len(clist) > 0, f"{len(clist)} 条")
    check("评论按点赞降序",
          all(clist[i]["likes"] >= clist[i + 1]["likes"] for i in range(len(clist) - 1)),
          str([c["likes"] for c in clist]))
    check("评论含昵称与内容",
          all(c["user"] and c["content"] for c in clist))

    # 视图与渲染：有数据就要出现对应区块
    ex_detail = await ex_plugin.client.fetch_match_detail("2399984")
    await ex_plugin._attach_extras(ex_detail)
    ex_view = await ex_plugin._build_view_with_images(ex_detail)
    check("视图带支持率", bool(ex_view["support"]), str(ex_view["support"]))
    check("视图带评论数", ex_view["comment_count"] > 0, str(ex_view["comment_count"]))
    ex_html = render_template(ex_plugin._tmpl, ex_view)
    check("模板渲染支持率区块", "战队支持率" in ex_html and "sup-bar" in ex_html)
    check("模板渲染评论数无残留", "{{" not in ex_html and "{%" not in ex_html)

    print("\n=== 16. 赛前预告推送 ===")
    umo5 = "qq:GroupMessage:777"
    called = []

    async def _fake_preview(umo, m, **kwargs):
        called.append(str(m.get("match_id")))

    orig_preview = plugin._push_preview_card
    plugin._push_preview_card = _fake_preview
    try:
        now = int(time.time())
        base_up = dict(plugin_mod.MOCK_MATCH_DATA)
        base_up["status"] = "upcoming"
        base_up["maps"] = []

        soon = dict(base_up, match_id="900002", start_time=now + 600)
        await plugin._process_match(umo5, soon)
        check("开赛前 10 分钟会推送", called == ["900002"], str(called))

        await plugin._process_match(umo5, soon)
        check("赛前预告只推一次", called == ["900002"], str(called))

        far = dict(base_up, match_id="900003", start_time=now + 7200)
        await plugin._process_match(umo5, far)
        check("距开赛 2 小时不推送", called == ["900002"], str(called))

        past = dict(base_up, match_id="900004", start_time=now - 300)
        await plugin._process_match(umo5, past)
        check("已过开赛时间会补推延后预告",
              called == ["900002", "900004"], str(called))

        # 关闭开关后不再推
        plugin.push_preview = False
        near2 = dict(base_up, match_id="900005", start_time=now + 300)
        await plugin._process_match(umo5, near2)
        check("关闭开关后不推送", called == ["900002", "900004"], str(called))
        plugin.push_preview = True
    finally:
        plugin._push_preview_card = orig_preview

    # 倒计时文案
    check("倒计时：分钟", plugin._countdown_text(int(time.time()) + 600).endswith("分钟后开始"),
          plugin._countdown_text(int(time.time()) + 600))
    check("倒计时：即将开始", plugin._countdown_text(int(time.time()) - 10) == "即将开始")
    check("倒计时：空时间不报错", plugin._countdown_text(0) == "")

    print("\n=== 17. 赛事订阅：字符串匹配 + LLM 兜底 ===")
    sub_plugin = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False, "demo_mode": True})
    names = ["IEM 北京 2026", "BLAST对抗赛 2026 S2", "PGL 2026 新加坡Major",
             "CCT 2026 欧洲系列赛 第12季"]

    check("字符串匹配：大小写不敏感",
          sub_plugin._match_event_names("iem", names) == ["IEM 北京 2026"],
          str(sub_plugin._match_event_names("iem", names)))
    check("字符串匹配：空关键词不匹配",
          sub_plugin._match_event_names("", names) == [])
    check("字符串匹配：中文关键词",
          sub_plugin._match_event_names("新加坡", names) == ["PGL 2026 新加坡Major"])

    class _Resp:
        def __init__(self, text):
            self.completion_text = text

    class _Provider:
        def __init__(self, reply, delay=0.0):
            self.reply, self.delay = reply, delay
            self.calls = 0

        async def text_chat(self, prompt=None, **kwargs):
            self.calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            return _Resp(self.reply)

    # 无 provider：直接跳过，不抛错
    check("无 LLM provider 时静默跳过",
          await sub_plugin._llm_match_events("英特尔", names, "qq:1") == [])

    # 默认 provider + Markdown 包裹的 JSON
    ctx2 = Context()
    ctx2.using_provider = _Provider('```json\n["IEM 北京 2026"]\n```')
    p2 = plugin_mod.WmpvpCsgoPlugin(ctx2, {"poll_enable": False, "demo_mode": True})
    got = await p2._llm_match_events("英特尔", names, "qq:1")
    check("LLM 解析 Markdown 包裹的 JSON", got == ["IEM 北京 2026"], str(got))
    check("LLM 确实被调用", ctx2.using_provider.calls == 1, str(ctx2.using_provider.calls))

    # 插件独立 provider 优先于默认
    ctx3 = Context()
    ctx3.using_provider = _Provider('["BLAST对抗赛 2026 S2"]')
    custom = _Provider('["IEM 北京 2026"]')
    ctx3.providers["custom-id"] = custom
    p3 = plugin_mod.WmpvpCsgoPlugin(
        ctx3, {"poll_enable": False, "demo_mode": True, "llm_provider_id": "custom-id"})
    got3 = await p3._llm_match_events("英特尔", names, "qq:1")
    check("插件独立 provider 生效", got3 == ["IEM 北京 2026"], str(got3))
    check("独立 provider 被调用", custom.calls == 1)
    check("默认 provider 未被调用", ctx3.using_provider.calls == 0)

    # 指定的 provider 不存在 → 回退默认
    ctx4 = Context()
    ctx4.using_provider = _Provider('["PGL 2026 新加坡Major"]')
    p4 = plugin_mod.WmpvpCsgoPlugin(
        ctx4, {"poll_enable": False, "demo_mode": True, "llm_provider_id": "no-such-id"})
    got4 = await p4._llm_match_events("major", names, "qq:1")
    check("provider 不存在时回退默认", got4 == ["PGL 2026 新加坡Major"], str(got4))

    # 超时 3 秒
    ctx5 = Context()
    ctx5.using_provider = _Provider('["IEM 北京 2026"]', delay=5.0)
    p5 = plugin_mod.WmpvpCsgoPlugin(ctx5, {"poll_enable": False, "demo_mode": True})
    t0 = time.time()
    got5 = await p5._llm_match_events("英特尔", names, "qq:1")
    cost = time.time() - t0
    check("LLM 超时静默跳过", got5 == [], str(got5))
    check("LLM 超时约 3 秒", 2.5 <= cost <= 4.5, f"{cost:.1f}s")

    # 非 JSON / 编造赛事名
    ctx6 = Context()
    ctx6.using_provider = _Provider("我觉得没有相关的赛事")
    p6 = plugin_mod.WmpvpCsgoPlugin(ctx6, {"poll_enable": False, "demo_mode": True})
    check("LLM 返回非 JSON 时为空",
          await p6._llm_match_events("英特尔", names, "qq:1") == [])
    ctx6.using_provider = _Provider('["不存在的编造赛事"]')
    check("LLM 编造的赛事名被过滤",
          await p6._llm_match_events("英特尔", names, "qq:1") == [])

    print("\n=== 18. 赛事订阅反馈文案 ===")
    ctx7 = Context()
    p7 = plugin_mod.WmpvpCsgoPlugin(ctx7, {"poll_enable": False, "demo_mode": True})
    p7._event_cache["gw"] = (time.time(), ["IEM 北京 2026", "CCT 2026 欧洲系列赛 第12季"])
    umo_e = "qq:GroupMessage:888"

    msg = await p7._cmd_sub_event(umo_e, "iem")
    check("命中时提示匹配到的赛事", "IEM 北京 2026" in msg, msg)
    check("命中时回显原始关键词", "`iem`" in msg, msg)
    check("原始关键词写入订阅",
          any(e.get("name") == "iem" for e in p7.subs.get(umo_e)["events"]),
          str(p7.subs.get(umo_e)["events"]))

    msg2 = await p7._cmd_sub_event(umo_e, "根本没有这个赛事")
    check("未命中时给出兜底文案", "暂未发现包含该关键词的赛事" in msg2, msg2)
    check("未命中也写入订阅",
          any(e.get("name") == "根本没有这个赛事" for e in p7.subs.get(umo_e)["events"]))

    msg3 = await p7._cmd_sub_event(umo_e, "iem")
    check("重复订阅有提示", "已订阅过" in msg3, msg3)

    # matched 全名参与命中判定
    entry = next(e for e in p7.subs.get(umo_e)["events"] if e.get("name") == "iem")
    check("匹配结果已持久化", entry.get("matched") == ["IEM 北京 2026"], str(entry))

    print("\n=== 19. 数据源配置与群内覆盖 ===")
    auto_p = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False, "demo_mode": True, "data_source": "auto"})
    check("auto 时挂多源", len(auto_p.client.sources) >= 1, str(len(auto_p.client.sources)))
    check("auto 不锁定", auto_p.client.locked is False)

    locked_p = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False, "data_source": "wmpvp"})
    check("指定源时只挂一个", len(locked_p.client.sources) == 1,
          str([s.source_id for s in locked_p.client.sources]))
    check("指定源时锁定不回退", locked_p.client.locked is True)
    check("默认源解析正确", locked_p.default_source == "gw", locked_p.default_source)

    umo_s = "qq:GroupMessage:999"
    before = (await run(locked_p, "/csgo 数据源", umo_s))[0].payload
    check("数据源指令显示当前设置", "本群指定" in before and "全局配置" in before, before[:60])

    setmsg = (await run(locked_p, "/csgo 数据源 5eplay", umo_s))[0].payload
    check("群内可指定数据源", "5EPlay" in setmsg, setmsg)
    check("群内指定已生效", locked_p._group_source(umo_s) == "5e")

    resetmsg = (await run(locked_p, "/csgo 数据源 auto", umo_s))[0].payload
    check("可恢复跟随全局", "恢复跟随全局" in resetmsg, resetmsg)
    check("恢复后覆盖被清除", locked_p._group_source(umo_s) == "")

    badmsg = (await run(locked_p, "/csgo 数据源 乱写", umo_s))[0].payload
    check("非法数据源有提示", "可选" in badmsg, badmsg)

    print("\n=== 20. 订阅显示全称 + 测试渲染多卡片 ===")
    p8 = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False, "demo_mode": True})
    umo_t = "qq:GroupMessage:1010"
    p8.subs.add_event(umo_t, "iem", ["IEM 北京 2026"])
    p8.subs.add_event(umo_t, "xyz")

    subs_text = p8._cmd_my_subs(umo_t)
    check("订阅列表显示全称", "IEM 北京 2026" in subs_text, subs_text)
    check("订阅列表附带关键词", "关键词：iem" in subs_text, subs_text)
    check("无匹配的订阅显示关键词", "· xyz" in subs_text, subs_text)

    # 按全名取消
    check("可按全名取消", p8.subs.remove_event(umo_t, "IEM 北京 2026") is True)
    check("取消后关键词也没了",
          not any(e.get("name") == "iem" for e in p8.subs.get(umo_t)["events"]),
          str(p8.subs.get(umo_t)["events"]))
    # 按关键词取消仍有效
    p8.subs.add_event(umo_t, "cct", ["CCT 2026 欧洲系列赛 第12季"])
    check("可按关键词取消", p8.subs.remove_event(umo_t, "cct") is True)

    # 测试渲染：赛前 + 逐图 + 总数据
    fake = {
        "match_id": "900010", "team_a": "Alpha", "team_b": "Beta",
        "team_a_id": "1", "team_b_id": "2",
        "score_a": 2, "score_b": 1, "status": "finished", "bo": 3,
        "event_name": "测试赛事", "stage": "决赛", "start_time": int(time.time()),
        "maps": [
            {"map_index": 0, "map_name": "Nuke", "score_a": 13, "score_b": 9,
             "status": "finished"},
            {"map_index": 1, "map_name": "Dust2", "score_a": 8, "score_b": 13,
             "status": "finished"},
            {"map_index": 2, "map_name": "Mirage", "score_a": 13, "score_b": 11,
             "status": "finished"},
        ],
        "players": [], "map_images": {}, "support_rate": {}, "rank_predict": {},
    }
    variants = await p8._build_render_variants(fake)
    check("变体数 = 1 赛前 + 3 图 + 1 总数据", len(variants) == 5, str(len(variants)))
    check("首张是赛前预告", variants[0][0] == "赛前预告", variants[0][0])
    check("末张是总数据", variants[-1][0] == "总数据", variants[-1][0])

    pre_view = variants[0][1]
    check("赛前卡不含地图与选手",
          pre_view["maps"] == [] and pre_view["a_players"] == [],
          f'{len(pre_view["maps"])}/{len(pre_view["a_players"])}')
    check("赛前卡带倒计时", bool(pre_view.get("countdown")), pre_view.get("countdown"))
    check("赛前卡比分归零",
          pre_view["score_a"] == 0 and pre_view["score_b"] == 0)

    scores = [(v["score_a"], v["score_b"]) for _, v in variants[1:4]]
    check("逐图卡大比分是累计值", scores == [(1, 0), (1, 1), (2, 1)], str(scores))
    check("逐图卡只含一张图", all(len(v["maps"]) == 1 for _, v in variants[1:4]))
    check("最后一张图标已结束",
          variants[3][1]["status_text"] == "已结束", variants[3][1]["status_text"])
    check("总数据卡含全部地图", len(variants[-1][1]["maps"]) == 3,
          str(len(variants[-1][1]["maps"])))

    # 5E 已结束比赛过滤未打的图
    raw_5e = {
        "mc_info": {"id": "csgo_mc_1", "format": "3", "plan_ts": "1793500000",
                    "t1_info": {"id": "t1", "disp_name": "A"},
                    "t2_info": {"id": "t2", "disp_name": "B"}},
        "state": {"status": "2", "t1_score": "2", "t2_score": "0",
                  "bout_states": [
                      {"bout_num": "1", "map_name": "Nuke", "status": "2",
                       "t1_score": "13", "t2_score": "5"},
                      {"bout_num": "2", "map_name": "Dust2", "status": "2",
                       "t1_score": "13", "t2_score": "9"},
                      {"bout_num": "3", "map_name": "Mirage", "status": "-1",
                       "t1_score": None, "t2_score": None},
                  ]},
        "tt_info": {"disp_name": "测试赛"},
    }
    five = plugin_mod.FivePlayClient()
    norm = five.normalize_match(raw_5e)
    check("已结束比赛过滤未打的图", len(norm["maps"]) == 2,
          str([(m["map_name"], m["score_a"]) for m in norm["maps"]]))

    print("\n=== 21. 逐图选手数据 + 数据来源页脚 ===")

    def _pr(name, k, d, adr="80.0", rt="1.10"):
        return {"name": name, "kill": str(k), "death": str(d), "adr": adr, "rating": rt}

    raw_map = {
        "mc_info": {"id": "csgo_mc_9", "format": "3", "plan_ts": "1793500000",
                    "t1_info": {"id": "t1", "disp_name": "A"},
                    "t2_info": {"id": "t2", "disp_name": "B"}},
        "state": {"status": "2", "t1_score": "2", "t2_score": "0",
                  "bout_states": [
                      {"bout_num": "1", "map_name": "Nuke", "status": "2",
                       "t1_score": "13", "t2_score": "5"},
                      {"bout_num": "2", "map_name": "Dust2", "status": "2",
                       "t1_score": "13", "t2_score": "9"},
                  ]},
        "bouts_state": [
            {"bout_num": "1", "map_name": "Nuke", "status": "2",
             "t1_pr_stats": [_pr("p1", 20, 10), _pr("p2", 15, 12)],
             "t2_pr_stats": [_pr("q1", 12, 18)]},
            {"bout_num": "2", "map_name": "Dust2", "status": "2",
             "t1_pr_stats": [_pr("p1", 9, 14), _pr("p2", 22, 11)],
             "t2_pr_stats": [_pr("q1", 16, 15)]},
        ],
        "tt_info": {"disp_name": "测试赛"},
        # 整场阵容顺序故意与逐图（按 Rating 降序）不同，用来验证重排
        "global_state": {
            "status": "2", "t1_score": "2", "t2_score": "0",
            "t1_player_stats": [_pr("p2", 37, 23), _pr("p1", 29, 24)],
            "t2_player_stats": [_pr("q1", 28, 33)],
        },
    }
    five2 = plugin_mod.FivePlayClient()
    m_map = five2.normalize_match(raw_map)
    five2._apply_players(m_map, raw_map)

    check("解析出逐图选手", set((m_map.get("map_players") or {}).keys()) == {0, 1},
          str(sorted((m_map.get("map_players") or {}).keys())))
    def _kd(mp, idx, nick):
        return next((x["kd"] for x in mp[idx] if x["nickname"] == nick), "?")

    check("图1 是单图数据", _kd(m_map["map_players"], 0, "p1") == "20-10",
          str(_kd(m_map["map_players"], 0, "p1")))
    check("图2 同一选手数据不同", _kd(m_map["map_players"], 1, "p1") == "9-14",
          str(_kd(m_map["map_players"], 1, "p1")))
    check("图1/图2 数据确实不同",
          _kd(m_map["map_players"], 0, "p1") != _kd(m_map["map_players"], 1, "p1"))
    check("整场阵容顺序为 p2,p1,q1",
          [x["nickname"] for x in m_map["players"]] == ["p2", "p1", "q1"],
          str([x["nickname"] for x in m_map["players"]]))
    check("逐图选手已按阵容顺序重排",
          [x["nickname"] for x in m_map["map_players"][1]] == ["p2", "p1", "q1"],
          str([x["nickname"] for x in m_map["map_players"][1]]))

    # 页脚随数据源变化
    check("5E 页脚正确",
          plugin_mod.WmpvpCsgoPlugin._footer_note({"source": "5e"})
          == "数据来源：5EPlay 赛事中心")
    check("wm 页脚正确",
          "完美世界数据中心" in plugin_mod.WmpvpCsgoPlugin._footer_note({"source": "wm"}))
    check("gw 页脚正确",
          "完美世界电竞" in plugin_mod.WmpvpCsgoPlugin._footer_note({"source": "gw"}))
    check("未知源有兜底",
          "数据来源" in plugin_mod.WmpvpCsgoPlugin._footer_note({}))

    # 地图卡使用逐图选手
    p9 = plugin_mod.WmpvpCsgoPlugin(
        Context(), {"poll_enable": False, "demo_mode": True})
    variants9 = await p9._build_render_variants(m_map)
    _card_kd = {x["nickname"]: x["kd"] for x in variants9[1][1]["a_players"]}
    check("地图卡用逐图选手", _card_kd.get("p1") == "20-10", str(_card_kd))
    _tot_kd = {x["nickname"]: x["kd"] for x in variants9[-1][1]["a_players"]}
    check("总数据卡用整场选手", _tot_kd.get("p1") == "29-24", str(_tot_kd))

    # 过滤未打的图 + 同步重排逐图索引
    raw3 = dict(raw_map)
    raw3["bouts_state"] = list(raw_map["bouts_state"]) + [
        {"bout_num": "3", "map_name": "Mirage", "status": "-1",
         "t1_pr_stats": [], "t2_pr_stats": []}]
    raw3["state"] = dict(raw_map["state"], bout_states=list(raw_map["state"]["bout_states"]) + [
        {"bout_num": "3", "map_name": "Mirage", "status": "-1",
         "t1_score": "0", "t2_score": "0"}])
    m3 = five2.normalize_match(raw3)
    five2._apply_players(m3, raw3)
    plugin_mod._drop_unplayed_maps(m3)
    check("未打的图被过滤", len(m3["maps"]) == 2,
          str([x["map_name"] for x in m3["maps"]]))
    check("过滤后逐图索引仍对齐",
          set((m3.get("map_players") or {}).keys()) == {0, 1},
          str(sorted((m3.get("map_players") or {}).keys())))

    print("\n=== 22. 开赛时间变动（提前开打 / 改期） ===")
    ctx9 = Context()
    p10 = plugin_mod.WmpvpCsgoPlugin(ctx9, {"poll_enable": False, "demo_mode": True})
    shots = []

    async def _rec(umo, m, started=False, delayed=False):
        kind = "started" if started else ("delayed" if delayed else "pre")
        shots.append((m.get("match_id"), kind))

    p10._push_preview_card = _rec
    base_up = dict(plugin_mod.MOCK_MATCH_DATA)
    base_up["maps"] = []
    now2 = int(time.time())
    umo_v = "qq:GroupMessage:2020"

    # 提前开打：先建基线，再以「已开赛 2 分钟」出现
    await p10._process_match(umo_v, dict(base_up, match_id="V1", status="upcoming",
                                         start_time=now2 + 7200))
    check("窗口外不推送", shots == [], str(shots))
    await p10._process_match(umo_v, dict(base_up, match_id="V1", status="live",
                                         start_time=now2 - 120))
    check("提前开打会补推", shots == [("V1", "started")], str(shots))

    # 正常进窗口只推一次
    shots.clear()
    await p10._process_match(umo_v, dict(base_up, match_id="V2", status="upcoming",
                                         start_time=now2 + 600))
    await p10._process_match(umo_v, dict(base_up, match_id="V2", status="live",
                                         start_time=now2 - 30))
    check("正常窗口只推一次", shots == [("V2", "pre")], str(shots))

    # 改期超过 30 分钟 → 重置，按新时间再推
    shots.clear()
    await p10._process_match(umo_v, dict(base_up, match_id="V3", status="upcoming",
                                         start_time=now2 + 300))
    check("改期前先推一次", shots == [("V3", "pre")], str(shots))
    await p10._process_match(umo_v, dict(base_up, match_id="V3", status="upcoming",
                                         start_time=now2 + 300 + 3600))
    check("改期后不立即重推", shots == [("V3", "pre")], str(shots))
    await p10._process_match(umo_v, dict(base_up, match_id="V3", status="upcoming",
                                         start_time=now2 + 600))
    check("按新时间再推一次", shots == [("V3", "pre"), ("V3", "pre")], str(shots))

    # 改期幅度小于阈值不重置（避免上游抖动导致重复推送）
    shots.clear()
    await p10._process_match(umo_v, dict(base_up, match_id="V4", status="upcoming",
                                         start_time=now2 + 300))
    await p10._process_match(umo_v, dict(base_up, match_id="V4", status="upcoming",
                                         start_time=now2 + 900))
    check("小幅时间抖动不重推", shots == [("V4", "pre")], str(shots))

    # 首次见到就是进行中：不补推（避免订阅瞬间刷屏）
    shots.clear()
    await p10._process_match(umo_v, dict(base_up, match_id="V5", status="live",
                                         start_time=now2 - 60))
    check("首次见到进行中不补推", shots == [], str(shots))

    # 已开赛文案
    txt = p10._preview_text(
        {"team_a": "A", "team_b": "B"}, {"countdown": "已开赛", "support": {}}, started=True)
    check("已开赛文案正确", "已开赛" in txt and "赛前预告" not in txt, txt)
    txt2 = p10._preview_text({"team_a": "A", "team_b": "B"},
                             {"countdown": "5 分钟后开始", "support": {}})
    check("赛前文案正确", "赛前预告" in txt2, txt2)

    print("\n=== 23. 串行赛程：前一场未结束则暂缓 ===")
    now3 = int(time.time())

    def _mk(mid, ev, st, status):
        return {"match_id": mid, "event_name": ev, "start_time": st, "status": status}

    rows_serial = [
        _mk("S1", "PGL 小组赛", now3 - 1200, "live"),      # 正在打，已超时 20 分钟
        _mk("S2", "PGL 小组赛", now3 + 300, "upcoming"),   # 原定 5 分钟后
        _mk("S3", "PGL 小组赛", now3 + 7200, "upcoming"),  # 两小时后
        _mk("X1", "另一赛事", now3 + 300, "upcoming"),      # 不同赛事，不受影响
    ]
    blocked = plugin_mod.WmpvpCsgoPlugin._compute_blocked(rows_serial)
    check("前一场在打 → 后一场被压住", blocked.get("S2") is True, str(blocked))
    check("级联：后面的比赛同样被压住", blocked.get("S3") is True, str(blocked))
    check("不同赛事互不影响", blocked.get("X1") is not True, str(blocked))
    check("进行中的比赛自己不算被压", blocked.get("S1") is not True, str(blocked))

    # 前一场结束后解除
    rows_free = [
        _mk("S1", "PGL 小组赛", now3 - 1200, "finished"),
        _mk("S2", "PGL 小组赛", now3 + 300, "upcoming"),
    ]
    check("前一场结束后解除",
          plugin_mod.WmpvpCsgoPlugin._compute_blocked(rows_free).get("S2") is not True)

    # 跨天/太久远的前序不算压住
    rows_far = [
        _mk("S0", "PGL 小组赛", now3 - 10 * 3600, "live"),
        _mk("S2", "PGL 小组赛", now3 + 300, "upcoming"),
    ]
    check("超过 6 小时的前序不压住",
          plugin_mod.WmpvpCsgoPlugin._compute_blocked(rows_far).get("S2") is not True)

    # blocked=True 时按住不推；解除后按「已延后」补推
    ctx10 = Context()
    p11 = plugin_mod.WmpvpCsgoPlugin(ctx10, {"poll_enable": False, "demo_mode": True})
    shots2 = []

    async def _rec2(umo, m, started=False, delayed=False):
        shots2.append((m.get("match_id"),
                       "started" if started else ("delayed" if delayed else "pre")))

    p11._push_preview_card = _rec2
    base2 = dict(plugin_mod.MOCK_MATCH_DATA)
    base2["maps"] = []
    umo_b = "qq:GroupMessage:3030"

    m_s2 = dict(base2, match_id="S2", status="upcoming", start_time=now3 + 300)
    await p11._process_match(umo_b, m_s2, blocked=True)
    check("被压住时不推预告", shots2 == [], str(shots2))

    await p11._process_match(umo_b, m_s2, blocked=False)
    check("解除后推正常预告", shots2 == [("S2", "pre")], str(shots2))

    # 原定时间已过 + 解除阻塞 → 推「已延后」
    shots2.clear()
    m_late = dict(base2, match_id="S4", status="upcoming", start_time=now3 - 1800)
    await p11._process_match(umo_b, m_late, blocked=True)
    check("延后且被压住仍不推", shots2 == [], str(shots2))
    await p11._process_match(umo_b, m_late, blocked=False)
    check("解除后按已延后补推", shots2 == [("S4", "delayed")], str(shots2))

    # 延后超过 2 小时不再补推
    shots2.clear()
    m_stale = dict(base2, match_id="S5", status="upcoming", start_time=now3 - 3 * 3600)
    await p11._process_match(umo_b, m_stale, blocked=False)
    check("延后超过 2 小时不补推", shots2 == [], str(shots2))

    # 文案
    t1 = p11._preview_text({"team_a": "A", "team_b": "B"},
                           {"countdown": "前一场刚结束，即将开始", "support": {}},
                           delayed=True)
    check("延后文案正确", "已延后" in t1, t1)

    print("\n=== 24. 推送故障回归 ===")

    # ① 指纹解析：match_id 自带冒号（5E 的 "5e:csgo_mc_xxx"）也必须取到 status
    for mid, expect in (("2399957", "live"),
                        ("5e:csgo_mc_2399957", "live"),
                        ("5e:csgo_mc_2399957:extra", "live")):
        fp = f"{mid}:0:{expect}:3:2"
        parts = fp.rsplit(":", 3)
        check(f"指纹解析 {mid[:18]}", parts[1] == expect, str(parts))

    # ② 5E ID 下地图状态流转能真的触发推送
    ctx11 = Context()
    p12 = plugin_mod.WmpvpCsgoPlugin(ctx11, {"poll_enable": False, "demo_mode": True})
    sent = []

    async def _text(u, t):
        sent.append(t)

    async def _card(u, m, item):
        sent.append("CARD")

    p12._push_text = _text
    p12._push_map_card = _card
    umo_r = "qq:GroupMessage:4040"
    now4 = int(time.time())
    base_r = {"match_id": "5e:csgo_mc_777", "team_a": "A", "team_b": "B",
              "event_name": "E", "start_time": now4 + 7200}

    def _item(status, mstatus, sa=0, sb=0):
        return dict(base_r, status=status,
                    maps=[{"map_index": 0, "map_name": "Nuke", "status": mstatus,
                           "score_a": sa, "score_b": sb}])

    await p12._process_match(umo_r, _item("live", "upcoming"))
    check("5E 建基线不推送", sent == [], str(sent))
    await p12._process_match(umo_r, _item("live", "live", 3, 2))
    check("5E 图开赛会推送", any("开赛" in x for x in sent), str(sent))
    sent.clear()
    await p12._process_match(umo_r, _item("finished", "finished", 13, 8))
    check("5E 图结束会推卡", "CARD" in sent, str(sent))

    # ③ 压制超时必须放行，不能永久不推
    ctx12 = Context()
    p13 = plugin_mod.WmpvpCsgoPlugin(ctx12, {"poll_enable": False, "demo_mode": True})
    shots3 = []

    async def _rec3(u, m, started=False, delayed=False, **kw):
        shots3.append(m.get("match_id"))

    p13._push_preview_card = _rec3
    umo_q = "qq:GroupMessage:5050"
    up_q = dict(base_r, match_id="Q1", status="upcoming", start_time=now4 + 300)
    await p13._process_match(umo_q, up_q, blocked=True)
    check("压制中不推", shots3 == [], str(shots3))
    rec = p13.state.get_match(umo_q, "Q1")
    rec["blocked_since"] = now4 - plugin_mod.MAX_BLOCK_WAIT - 1
    p13.state.set_match(umo_q, "Q1", rec)
    await p13._process_match(umo_q, up_q, blocked=True)
    check("压制超时后放行推送", shots3 == ["Q1"], str(shots3))
    check("放行后标记不再重复压制",
          bool(p13.state.get_match(umo_q, "Q1").get("block_released")))

    # ④ 诊断指令
    out_diag = (await run(p12, "/csgo 诊断"))[0].payload
    check("诊断含轮询状态", "轮询任务" in out_diag, out_diag[:80])
    check("诊断含渲染能力", "渲染：" in out_diag)
    check("诊断含订阅数", "订阅会话" in out_diag)
    check("诊断含数据源", "挂载源" in out_diag)

    # ⑤ 图片组件构造：URL 与本地文件都不能抛
    tmp_img = Path(tempfile.mkdtemp()) / "t.png"
    tmp_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    try:
        p12._make_image(str(tmp_img))
        local_ok = True
    except Exception:
        local_ok = False
    check("本地图片可构造组件", local_ok)
    try:
        p12._make_image("https://example.com/x.png")
        url_ok = True
    except Exception:
        url_ok = False
    check("URL 图片可构造组件", url_ok)
    check("Chrome 兜底渲染已接入", hasattr(p12, "_render_via_chrome"))
    check("能找到本机浏览器", bool(p12._find_chrome()), p12._find_chrome())

    print("\n=== 25. 跨源赛事名匹配 + 诊断订阅明细 ===")
    hit = plugin_mod.WmpvpCsgoPlugin._match_hit
    norm = plugin_mod._norm

    check("缩写：Intel Extreme Masters → iemb",
          plugin_mod._acronym("Intel Extreme Masters BeiJing 2026") == "iemb",
          plugin_mod._acronym("Intel Extreme Masters BeiJing 2026"))

    # 完美系用英文全名，5E 用中文简称，订阅关键词要两边都能命中
    m_en = {"event_name": "Intel Extreme Masters BeiJing 2026",
            "team_a": "FaZe", "team_b": "Vitality"}
    m_zh = {"event_name": "IEM 北京 2026", "team_a": "A", "team_b": "B"}
    check("iem 命中英文全名（缩写）", hit(m_en, [], [norm("iem")]))
    check("iem 命中中文简称", hit(m_zh, [], [norm("iem")]))
    check("中文全名也能命中英文赛事", hit(m_en, [], [norm("IEM 北京 2026")]))
    check("无关关键词不误命中", not hit(m_en, [], [norm("xyz不存在")]))
    check("赛事不匹配时不影响队伍匹配",
          hit(m_en, [norm("faze")], [norm("xyz")]))
    check("cct 命中 CCT 赛事",
          hit({"event_name": "CCT 2026 Europe Series 14"}, [], [norm("cct")]))

    # 诊断要能列出订阅项与命中数
    ctx13 = Context()
    p14 = plugin_mod.WmpvpCsgoPlugin(ctx13, {"poll_enable": False, "demo_mode": True})
    umo_d = "qq:GroupMessage:6060"
    p14.subs.add_team(umo_d, "FaZe")
    p14.subs.add_event(umo_d, "iem", ["IEM 北京 2026"])
    diag_text = await p14._cmd_diag()
    check("诊断列出订阅明细", "【订阅明细】" in diag_text, diag_text[:120])
    check("诊断列出队伍订阅", "队伍「FaZe」" in diag_text, diag_text[:200])
    check("诊断列出赛事订阅", "赛事「iem」" in diag_text, diag_text[:200])
    check("诊断给出命中数", "命中" in diag_text)
    check("诊断显示渲染状态", "上次渲染" in diag_text)

    print("\n=== 26. 首次见到进行中比赛的即时反馈 ===")
    # 这组要精确统计推送条数，用独立数据目录，避免读到前面测试留下的订阅会话
    _saved_data_dir = plugin_mod._data_dir
    plugin_mod._data_dir = lambda: Path(tempfile.mkdtemp(prefix="wmpvp_h_"))
    ctx14 = Context()
    p15 = plugin_mod.WmpvpCsgoPlugin(ctx14, {"poll_enable": False, "demo_mode": True})
    umo_h = "qq:GroupMessage:7070"
    p15.subs.add_event(umo_h, "iem", ["IEM 北京 2026"])
    now5 = int(time.time())

    def _mk_live(mid, st="live"):
        return {"match_id": mid, "team_a": "A", "team_b": "B",
                "event_name": "Intel Extreme Masters BeiJing 2026",
                "status": st, "score_a": 1, "score_b": 0,
                "start_time": now5 - 3600, "maps": []}

    await p15._dispatch([_mk_live("H1")])
    check("首轮推一条赛况", len(ctx14.sent) == 1, str(len(ctx14.sent)))
    check("赛况文案正确", "你订阅的比赛" in ctx14.sent[0][1].chain[0].text,
          ctx14.sent[0][1].chain[0].text[:60])

    ctx14.sent.clear()
    await p15._dispatch([_mk_live("H1")])
    check("第二轮不重复推赛况", len(ctx14.sent) == 0, str(len(ctx14.sent)))

    # 一轮里多场进行中，最多 2 条，防刷屏
    ctx14.sent.clear()
    await p15._dispatch([_mk_live("H2"), _mk_live("H3"), _mk_live("H4")])
    check("首次反馈限额 2 条", len(ctx14.sent) == 2, str(len(ctx14.sent)))

    # 已结束比赛首轮给赛况后，不再补发「比赛结束」
    ctx14.sent.clear()
    await p15._dispatch([_mk_live("H5", "finished")])
    check("已结束首轮只发赛况", len(ctx14.sent) == 1, str(len(ctx14.sent)))
    ctx14.sent.clear()
    await p15._dispatch([_mk_live("H5", "finished")])
    check("已结束不重复补发赛果", len(ctx14.sent) == 0, str(len(ctx14.sent)))

    diag2 = await p15._cmd_diag()
    check("诊断列出命中比赛", "【命中比赛】" in diag2, diag2[:150])
    check("诊断列出命中比赛段落", "【命中比赛】" in diag2, diag2[:300])
    check("诊断列出下一轮间隔", "下一轮间隔" in diag2)
    plugin_mod._data_dir = _saved_data_dir

    print("\n=== 27. 整场结束推总数据卡 ===")
    _saved_dd = plugin_mod._data_dir
    plugin_mod._data_dir = lambda: Path(tempfile.mkdtemp(prefix="wmpvp_sum_"))
    ctx15 = Context()
    p16 = plugin_mod.WmpvpCsgoPlugin(ctx15, {"poll_enable": False, "demo_mode": True})
    umo_s = "qq:GroupMessage:8080"

    summary = {
        "match_id": "SUM1", "team_a": "The MongolZ", "team_b": "Natus Vincere",
        "score_a": 2, "score_b": 0, "status": "finished", "bo": 3,
        "event_name": "Intel Extreme Masters BeiJing 2026",
        "start_time": int(time.time()) - 7200,
        "maps": [{"map_index": 0, "map_name": "Cache", "status": "finished",
                  "score_a": 13, "score_b": 10}],
        "players": [],
    }

    # 渲染不可用 → 回退纯文字，内容仍完整
    await p16._push_match_summary(umo_s, summary)
    check("赛果卡渲染失败时回退文字", len(ctx15.sent) == 1, str(len(ctx15.sent)))
    fallback_text = ctx15.sent[0][1].chain[0].text
    check("回退文字含比分与胜者",
          "2 : 0" in fallback_text and "The MongolZ" in fallback_text, fallback_text[:80])
    check("回退文字含赛事名", "Intel Extreme Masters" in fallback_text)

    # 渲染可用 → 发带图消息，并计入图片推送统计
    ctx15.sent.clear()
    fake_png = Path(tempfile.mkdtemp()) / "sum.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    async def _fake_render(tmpl, data):
        return str(fake_png)

    p16._render_html_to_image = _fake_render
    before_img = p16._diag["push_images"]
    await p16._push_match_summary(umo_s, summary)
    check("渲染成功时只发一条", len(ctx15.sent) == 1, str(len(ctx15.sent)))
    check("渲染成功时消息带图片组件",
          len(ctx15.sent[0][1].chain) == 2, str(len(ctx15.sent[0][1].chain)))
    check("图片推送计数 +1", p16._diag["push_images"] == before_img + 1,
          str(p16._diag["push_images"]))
    check("最近推送标注总数据卡",
          "总数据卡" in p16._diag["last_push_note"], p16._diag["last_push_note"])

    # 平局文案
    ctx15.sent.clear()
    p16._render_html_to_image = lambda tmpl, data: _async_none()
    tie = dict(summary, score_a=1, score_b=1)
    await p16._push_match_summary(umo_s, tie)
    check("平局文案正确", "平局" in ctx15.sent[0][1].chain[0].text,
          ctx15.sent[0][1].chain[0].text[:80])

    plugin_mod._data_dir = _saved_dd

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：", FAIL)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
