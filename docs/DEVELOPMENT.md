# astrbot_plugin_wmpvp_csgo

完美世界电竞 CSGO 赛事助手（AstrBot 插件）。赛事查询、队伍/赛事订阅、地图级状态推送、三级渲染降级、多群隔离，内置开发者调试指令集。

内置 **三个数据源**，按优先级自动回退，全部**无需 Token**。

## 数据源（2026-11 实测逆向）

| 源 | 域名 | 鉴权 | 地图级数据 | 选手数据 | 实时比分 |
| --- | --- | --- | --- | --- | --- |
| `5eplay` | `esports-data.5eplaycdn.com` | **无** | 列表自带 | 详情 | 列表自带 |
| `wanmei_data` | `esports.wanmei.com/apg/...` | **HMAC-SHA256 动态签名** | 详情 | 详情 | 不支持 |
| `wmpvp` | `gwapi.pwesports.cn` | **无** | 详情 | 详情 | 支持 |

优先级在配置项 `data_source_priority` 里调整，默认 `5eplay,wanmei_data,wmpvp`。

### 5EPlay 赛事中心

```bash
# 列表（一次上百场，自带逐图比分）
curl 'https://esports-data.5eplaycdn.com/v1/api/csgo/matches?page=1&limit=100' \
  -H 'Referer: https://event.5eplay.com/'

# 详情（含 10 名选手的 K-D / ADR / Rating）
curl 'https://esports-data.5eplaycdn.com/v1/api/csgo/matches/csgo_mc_2399901/data' \
  -H 'Referer: https://event.5eplay.com/'
```

**鉴权：无。** 不带 token、sign、cookie，仅需 `Referer`。
社区提到的 `Authorization: Bearer` 与 `gate.5eplay.com/crane/http/api/data` 是 App 端接口，网页赛事中心并不使用，实测 `gate.5eplay.com` 该路径 404，故本文不采用。

字段映射：

| 插件字段 | 真实字段 |
| --- | --- |
| `match_id` | `mc_info.id`（加 `5e:` 前缀：`5e:csgo_mc_2399901`） |
| 队伍 | `mc_info.t1_info.disp_name` / `t2_info.disp_name` |
| 队徽 | `t1_info.logo`（`oss.5eplay.com`，需 Referer） |
| 大比分 | `state.t1_score` / `t2_score` |
| `status` | `state.status`：**0=未开始 1=进行中 2=已结束**；详情未打的图是 `-1` |
| `bo` | `mc_info.format`（`3`=BO3，`5`=BO5） |
| 赛事名 | `tt_info.disp_name`（中文）/ `abbr_zh` |
| `start_time` | `mc_info.plan_ts`（秒） |
| `maps` | `state.bout_states[]`：`map_name` / `t1_score` / `t2_score` / `status` |
| `players` | 详情 `match.global_state.t1_player_stats` / `t2_player_stats` |

**坑**：详情里的 `bouts_state[].t1_score` 恒为 `null`，回合比分只在**列表**里有；插件会把列表比分合并进详情（`_merge_list_scores`）。

### 完美世界数据中心

```bash
# 需要三个签名头，X-Signature 每次请求都不同
curl 'https://esports.wanmei.com/apg/eventcenter/csgo/getMatchList?matchTime=2026-11-01+00:00:00' \
  -H 'Referer: https://data.wanmei.com/' \
  -H 'X-Timestamp: 1793525460' \
  -H 'X-Nonce: a57a481d924443dc40d17c5534cb44ef' \
  -H 'X-Signature: <HMAC-SHA256 十六进制>'
```

**鉴权：动态签名**（密钥写死在 JS 里，无有效期、无 token 过期问题）。算法逆向自 `asset/csgo/js/csgo-*.js`：

```text
待签串 = 参数按键名升序，同名多值内部再排序，
        每项拼成 enc(k)=enc(v)，用 & 连接，
        并额外并入 timestamp 与 nonce 两个键
X-Signature = HMAC-SHA256(待签串, WANMEI_SIGN_KEY).hex()
enc(s) = encodeURIComponent(s) 后把 %20 换成 +，! ' ( ) * 转成 %XX
```

插件里由 `WanmeiDataClient.sign_headers()` 逐请求生成，用户无需配置任何密钥。

**重要发现**：该网关与 `gwapi.pwesports.cn` **同源同数据**（同 `matchId` 返回同样的 `statsDTOList` 与比分），差别只是域名、路径前缀（`/apg/eventcenter/csgo/`）和强制签名。所以它作为独立域名的备用线路有意义，但不是"另一份数据"。

**限制**：`/apg` 下没有实时比分接口（`getMatchPlayerScoreListRealtime` 返回 404）。不过插件在取实时数据时会沿着**完美系**（wm → gw）继续尝试，所以即使用该源做列表/详情，实时比分仍会由 `gwapi` 补上；只有两个完美系域名都不可用时才会丢失「当前打到第几图」。

### 完美电竞网关

```bash
curl 'https://gwapi.pwesports.cn/eventcenter/app/csgo/event/getMatchList?matchTime=2026-11-01+00:00:00' \
  -H 'Referer: https://news.wmpvp.com/'
```

无鉴权，列表 / 详情 / 实时三个接口齐全，是唯一支持实时比分（`boxNum` 表示当前打到第几张图）的源。

### 扩展数据：支持率 / VRS / 评论

这三个接口都挂在 `gwapi.pwesports.cn` 下，**同样无需鉴权**（评论接口的 `token` 传空串即可）。

```bash
# 评论总数
curl 'https://gwapi.pwesports.cn/appuser/community/comment/getCommentCount?entityId=2399984&entityType=8&token=' \
  -H 'Referer: https://news.wmpvp.com/'

# 热门评论（sort=4 为官方默认热度排序）
curl 'https://gwapi.pwesports.cn/appuser/community/comment/getCommentList?commentId=0&entityId=2399984&entityType=8&pageNum=1&pageSize=5&sort=4&token=&type=1&onlyOwner=false' \
  -H 'Referer: https://news.wmpvp.com/'

# VRS 积分变动预测
curl 'https://gwapi.pwesports.cn/eventcenter/app/csgo/event/getRankScoreChangePredict?matchId=2399984' \
  -H 'Referer: https://news.wmpvp.com/'
```

| 能力 | 来源 | 说明 |
| --- | --- | --- |
| 战队支持率 | 详情 `teamSupportRateMap` | 键是**队伍 ID**，值是该队票数；插件按两队 ID 取并归一化成百分比，两边合计恒为 100 |
| VRS 积分 | `getRankScoreChangePredict` | `initialRankings` 给当前分与全球排名，`ifTeam1Wins` / `ifTeam2Wins` 给四个分支的 `scoreChange` |
| 评论 | 社区 `getCommentCount` / `getCommentList` | `commentDTOS[]` 里的 `userName` / `content` / `likeCount` / `createTimeText` |

几个实现上的取舍：

1. **VRS 只对未结束的比赛展示**。比赛结束后该接口会返回 0 或残留值，预测已无意义，插件直接不渲染该区块。
2. **加减分取四个分支的真实值，不做正负对称假设**。实测 Metizport vs JiJieHao 是「胜 +34 / 负 -8」「胜 +10 / 负 -21」，先验地取相反数会算错。
3. **支持率缺任一侧就不展示**，避免把别的比赛的比率错配过来。
4. **评论只取首页并按点赞降序**，正文截断到 120 字；带图的评论会标「🖼 含图」，但不下图。
5. **跨源映射**：默认优先 5EPlay，而这三个接口只在完美系有。插件会用「开赛时间 ±15 分钟 + 队名相似度（正序/倒序取较大）」把 5E 的比赛映射到完美电竞的同场 ID，再取扩展数据。匹配不到就静默省略对应区块——宁可少显示，也不挂错数据。

### 串行赛程：前一场没打完，下一场就一直等

PGL / ESL 这类单舞台赛事是**串行**的：上一场打不完，下一场就干等；前面结束太快，
后面的也可能提前开。所以「原定开赛时间」在单舞台赛事里基本只能当参考。

处理方式：每次轮询先算出「哪些比赛正被前一场压着」（`_compute_blocked`），被压住的
**不推预告**，等前一场结束的下一轮再推：

```text
同一 event_name 下，存在 start_time 更早且仍在进行中的比赛
  → 当前这场 blocked = True（级联：更靠后的比赛同样被压住）
  → 预告按住不发，避免用户收到「5 分钟后开始」然后干等一小时
```

解除阻塞时按两种情况补推：

| 解除时的状态 | 推什么 |
| --- | --- |
| 原定时间还没到 | 正常预告（「X 分钟后开始」） |
| 原定时间已过 | 延后预告（「🔔 赛前预告（已延后）」+「前一场刚结束，即将开始」） |

两个阈值：

- `BLOCK_SPAN = 6h`：只把 6 小时内的前序比赛视为同一时段，避免跨天比赛互相压制
- `DELAYED_GRACE = 2h`：延后超过 2 小时就不再补推（那时可能已经打了一半，再发预告是误导）

### 开赛时间会变（提前开打 / 改期）

赛事方经常临时调整开赛时间，赛前预告做了三处兜底：

| 情况 | 处理 |
| --- | --- |
| 正常进入「开赛前 N 分钟」窗口 | 推预告，只推一次 |
| **提前开打**（还没到窗口就 LIVE） | 开赛后 10 分钟内补推一张「已开赛」，否则这场的阵容/VRS/热评整场都看不到 |
| **改期**（开赛时间变动 > 30 分钟） | 重置预告标记，按新时间再推一次；变动 < 30 分钟视为上游抖动，不重推 |
| 订阅时比赛已经在打 | 不补推，避免订阅瞬间刷屏 |

状态里额外存了 `preview_start`（推预告时的开赛时间），用来判断后续是否发生了改期。

### 逐图数据与数据来源

地图结束卡片展示的是**这一张图**的选手数据，不是整场汇总：

| 数据 | 5EPlay | 完美系 |
| --- | --- | --- |
| 整场选手 | `match.global_state.t1/t2_player_stats` | `statsDTOList[0]`（总览段） |
| 逐图选手 | `bouts_state[].t1/t2_pr_stats` | `statsDTOList[i]`（逐图段） |

两个坑：

1. **逐图数据是按 Rating 降序返回的**，不重排的话同一名选手在图1/图2 会跳到不同行，
   看起来像换了人。`_reorder_map_players()` 按整场阵容顺序重排。
2. **5EPlay 详情会把没打的决胜图也列出来（0:0）**，而且它的 `bouts_state` 不带回合比分，
   得先合并列表接口的比分才能判断哪张图真打过 —— 所以 `_drop_unplayed_maps()` 必须在
   `_merge_list_scores()` **之后**调用，否则全是 0:0 判断不出来。

页脚的数据来源随实际使用的源变化（5EPlay / 完美世界数据中心 / 完美世界电竞），不再写死。

### 赛事订阅：字符串匹配 + LLM 语义兜底

`/csgo 赛事订阅 iem` 不再只回一句"已订阅"，而是先算出近期真实存在的赛事，再告诉你匹配到了哪些。

**匹配流程**

```text
1) 拉近期赛事日历（过去 7 天 ~ 未来 30 天，38 次按天请求并发拉取，缓存 5 分钟）
2) 代码匹配：关键词与赛事全名做不区分大小写的子串匹配
3) 代码为空 → LLM 语义兜底（3 秒超时，失败静默跳过）
4) 无论是否命中，都把「原始关键词」写入订阅
```

**LLM 兜底**

```text
用户想要订阅与"{keyword}"相关的电竞赛事。以下是近期真实的赛事列表：
["IEM 北京 2026", "BLAST对抗赛 2026 S2", ...]
请从中挑选出用户可能想订阅的具体赛事全名。如果没有任何相关的，请返回空数组。
请严格以 JSON 数组格式输出，例如：["赛事名1", "赛事名2"]。不要包含任何其他解释文字。
```

- 用 `re.search(r"\[.*\]", text, re.S)` 剥离可能存在的 Markdown 代码块
- 只接受**确实存在于候选列表**里的名字，模型编造的会被丢弃
- `asyncio.wait_for(..., timeout=3.0)`，超时/解析失败一律返回空数组，不阻塞订阅

**Provider 选择优先级**

```text
插件配置 llm_provider_id  →  context.get_provider_by_id(id)
   （为空或取不到）        →  context.get_using_provider(umo=...)
   （仍取不到）            →  跳过 LLM 兜底，直接返回「暂未发现」
```

**反馈文案**

```text
✅ 已为本群订阅赛事关键词：`iem`
📌 近期匹配到的赛事：IEM 北京 2026
以上赛事的后续动态将推送到本会话。
```

```text
✅ 已为本群订阅赛事关键词：`xyz`
🔍 当前近期赛程中暂未发现包含该关键词的赛事。
一旦有新赛事发布，将自动推送。
```

匹配到的赛事全名会一起存进订阅记录的 `matched` 字段，后续推送判定时「关键词」和「全名」任一命中都算（关键词本身未必是全名的子串，比如「英特尔」→「IEM 北京 2026」）。

### 数据源优先级与群内覆盖

```text
群内指令 /csgo 数据源 <源>   ← 最高
WebUI 配置 data_source       ← 次之
auto（5eplay → wanmei_data → wmpvp）  ← 兜底
```

- `data_source=auto`：挂全部源，失败自动回退
- `data_source=<具体源>`：**只挂该源**，`MultiSourceClient(locked=True)`，失败原样抛错，绝不静默切换
- 群内覆盖存在 `data/group_source.json`，只影响该群的**查询类指令**；后台轮询仍用全局配置

### 多源回退

```text
fetch_match_list()
  ├─ 5eplay   成功 → 用它
  ├─ wanmei_data 成功 → 用它
  └─ wmpvp    成功 → 用它
  全部失败 → ApiError("所有数据源均失败：…（逐源原因）")
```

- 成功的源会被记为 `active`，下一轮从它开始试，避免每次都从第一个撞。
- 比赛 ID 带源前缀（`5e:csgo_mc_xxx`），详情/实时按前缀路由回对应源；完美系两源 ID 同源，保持裸数字，不会让已有的订阅状态失效。
- 任一源恢复后会自动重新顶上，不需要重启。

## 推送不工作？先跑 `/csgo 诊断`

这个指令会把「为什么不推送」一次性摊开，不用翻日志：

```text
🔍 推送诊断
轮询任务：运行中 / 未运行
累计轮询：N 次 ｜ 上次：Xs 前
上次取到 N 场，订阅命中 N 场        ← 命中 0 说明订阅关键词没对上
累计推送：N 条（图片 N / 纯文字 N）  ← 纯文字多说明渲染有问题
订阅会话：N 个 ｜ 订阅项：N 条       ← 0 条则后台根本不会推
数据源：全局=auto ｜ 当前=5e
渲染：html_render=可用 / Playwright=未安装 / Chrome=找到
```

`/csgo 诊断` 还会列出**每条订阅的命中场次**，命中 0 时给出最接近的队名/赛事名：
「今天确实没这队比赛」和「订阅名字写错了」一眼就能分开。

另外新增【命中比赛】段落，逐场说明「这轮为什么没推」：首轮建基线 / 地图状态变化时推 /
约 N 分钟后进预告窗口 / 被前序比赛压住，并显示下一轮间隔。

### 整场结束推「总数据卡」

早期 `_push_match_summary` 只发一条纯文字，而 `/csgo 测试 渲染` 里明明有总数据卡，
两边不一致。现在整场结束也会推图：全部地图比分 + 双方 5v5 + 战队支持率，
文字简报作为图片说明附在消息里；渲染链路全部失败时才退回原来的纯文字。

带图的推送一共三种，都走同一套四级渲染降级：

| 时机 | 内容 |
| --- | --- |
| 开赛前 N 分钟 | 赛前预告卡：对阵、倒计时、支持率、VRS、热评 |
| 每张图结束 | 单图卡：该图比分 + **该图**的 5v5 数据 |
| 整场结束 | 总数据卡：全部地图 + 整场 5v5 + 支持率 |

### 订阅后不再全程静默

早期策略是「首次见到比赛只建基线、不推送」，避免订阅瞬间刷屏。但代价是：
如果订阅时比赛**已经在打或已经打完**，它不会再有地图状态变化，
于是这场比赛从头到尾一条消息都不会发——用户会以为插件坏了。

现在首轮对「进行中 / 已结束」的命中外比赛会发一条即时赛况简报：

```text
📌 你订阅的比赛
The MongolZ 1 : 0 Natus Vincere
🔴 正在进行
Intel Extreme Masters BeiJing 2026
后续动态会继续推送到本会话。
```

每轮每会话**最多 2 条**，防止订阅热门赛事时刷屏；已结束的比赛发过简报后
会直接标记赛果已推，不会下一轮再补一条重复的「比赛结束」。

`/csgo 诊断 渲染` 会实测跑一次渲染链路，直接告诉你是成功还是退化成文字；
诊断里的「上次渲染」会记录 html_render / Playwright / Chrome 每一级的实际结果，
不再只报告「包是否安装」。

### 订阅跨源命名差异

同一赛事在不同数据源里名字不一样：5EPlay 叫 `IEM 北京 2026`，
完美系叫 `Intel Extreme Masters BeiJing 2026`。早期只做子串匹配，
用户订阅 `iem` 在 5E 源能命中，切到完美系后就成了 0 场。

现在赛事匹配额外做两层：

1. **首字母缩写**：`Intel Extreme Masters BeiJing 2026` → `iemb`，`iem` 是其前缀即命中
2. **拉丁字母段抽取**：`IEM 北京 2026` 归一化后是 `iem北京2026`，
   抽出 `iem` 再与缩写比对，中英混排的全名也能命中

队伍订阅的 `aliases` 也一起参与匹配了。

轮询每轮也会在日志里打一行摘要（场次 / 命中 / 累计推送 / 图片与文字条数 / 下次间隔），
方便在 AstrBot 日志里确认后台到底有没有在跑。

### 已修的三个「不推送」根因

1. **地图级推送全失效**：状态指纹是 `match_id:idx:status:sa:sb`，代码用 `split(":")[2]`
   取上一状态。5EPlay 的 ID 是 `5e:csgo_mc_xxx`（自带冒号），索引 2 取到的是 `idx`
   而不是 `status`，导致 `upcoming→live`、`live→finished` 判断永远不成立。
   改成 `rsplit(":", 3)[1]` 从尾部取，且**完全兼容已有的 state.json 数据**，无需迁移。

2. **赛前预告被永久压住**：单舞台赛程的「前一场还在打就暂缓」判定没有上限，
   上游 live 状态一旦卡住，后面所有比赛永远不推预告。现在最多压 30 分钟
   （`MAX_BLOCK_WAIT`），超时强制放行并记 `block_released`，不再被反复压住。

3. **预告只有文字没有图**：`html_render` 可能返回 http URL，而消息构造只认本地文件
   （`Image.fromFileSystem`），三种构造全失败后异常冒泡，上层静默退化成纯文字。
   现在 URL 会先下载成本地 PNG，`_make_image` 支持 URL / base64 / 本地文件与多种
   AstrBot 版本构造方式；另外新增**第四级渲染兜底**：直接调本机 Chrome/Edge headless
   截图，不依赖 `html_render` 也不依赖 `playwright` 包。

## 接口逆向结论（原 `api.wmpvp.com`）

`api.wmpvp.com/api/v1/csgo/match` 会稳定返回 `{"statusCode":14,"errorMessage":"参数错误"}` —— 那是 App 端签名接口，网页从未使用，因此插件不采用。

网页真实使用的是 `gwapi.pwesports.cn`（见上），通过 Chrome netlog 抓包定位。

### wmpvp 源字段映射

统一信封 `{"code":0,"message":"success","result":{...}}`。

| 插件字段 | 真实字段 |
| --- | --- |
| `match_id` | `matchId` |
| `team_a` / `team_b` | `team1DTO.name` / `team2DTO.name` |
| `team_a_logo` | `team1DTO.logoBlack` |
| `score_a` / `score_b` | `score1` / `score2`（大比分，即赢下的地图数） |
| `status` | `status`：**1=进行中 2=未开始 3=已结束** |
| `start_time` | `startTime`（毫秒时间戳） |
| `bo` | `bo`：字符串 `"bo3"` / `"bo5"` |
| `event_name` | `csgoEventDTO.name`（另有 `nameZh` 中文名） |
| `stage` | `stageName` |
| `maps[]` | 详情 `statsDTOList[1:]`：`mapDTO.nameEn` + `score1/score2` |
| `players[]` | 详情 `statsDTOList[0]`（`nameEn == "all"` 的总览段）的 `team1/2PlayerStatsDTOList` |
| 当前地图 | 实时接口 `boxNum` + `mapName`（中文）→ 映射回英文 |

几个容易踩的坑，代码里都已处理：

1. **列表接口不含地图与选手**，`mapBPDTOS` / `statsDTOList` 在列表里恒为 `null`，必须再请求详情。
2. **详情里的 `statsDTOList[0]` 是"总览"段**（`mapDTO.nameEn == "all"`），逐图数据从下标 1 开始。
3. **`completedStatus` 为 1~4 的比赛是取消/延期**，前端会过滤掉，插件同样跳过。
4. **进行中的比赛 `statsDTOList` 可能为空**，此时只能靠实时接口的 `boxNum` 判断打到第几张图。
5. **CDN 的 `content-type` 会写错**（WEBP 报成 `image/jpg`），内联时按文件魔数判断真实类型。
6. **头像原图 100KB+**，用 OSS 的 `?x-oss-process=image/resize,m_fixed,w_96` 在服务端缩放，单张降到 1~3KB，整卡 HTML 从 1.7MB 压到 60KB 量级。

## 功能

| 能力 | 说明 |
| --- | --- |
| 赛事查询 | 按天查赛程（今天/明天/任意日期）、比分、单场高保真数据卡 |
| 战队支持率 | 数据卡内展示双方支持比例（来自完美电竞社区投票） |
| VRS 积分 | 数据卡内展示当前 VRS 分、全球排名、胜/负的加减分预测 |
| 热门评论 | `/csgo 评论` 拉取该场热评（按点赞排序），卡片上标注评论总数 |
| 赛前预告 | 开赛前 N 分钟自动推一张卡：谁打谁 + 倒计时 + 支持率 + VRS + 热评 |
| 队伍订阅 | 支持缩写与中文别名（`Faze`、`小蜜蜂`、`LVG`、`天禄`） |
| 赛事订阅 | 支持带空格全名与模糊匹配（`IEM 成都`、`blast`） |
| 地图级推送 | 图X开赛文字通知、图X结束数据卡、整场结束总结 |
| 多群隔离 | 订阅与推送全部按 `unified_msg_origin` 隔离，互不串群 |
| 三级降级 | `html_render` → 本地 Playwright → 纯文本 |
| 自适应轮询 | 进行中 60s / 今日有赛 300s / 空闲 600s / 夜间 1800s，失败指数退避 |

## 安装

1. 将整个 `astrbot_plugin_wmpvp_csgo` 目录放入 AstrBot 的 `data/plugins/` 下。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. （可选，第二级渲染）安装 Playwright 与 Chromium：

```bash
pip install playwright
playwright install chromium
```

4. 重启 AstrBot，在 WebUI 插件管理中启用并配置。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/csgo` | 帮助 |
| `/csgo 赛程` | 今日赛程 |
| `/csgo 赛程 明天` | 指定日期，支持 `今天/明天/后天/昨天/YYYY-MM-DD` |
| `/csgo 详情 <比赛ID>` | 高保真白底数据卡（含支持率、VRS、评论数） |
| `/csgo 评论 <比赛ID> [条数]` | 该场热评，按点赞降序，默认 5 条、最多 20 条 |
| `/csgo 数据源 [auto\|5eplay\|wanmei_data\|wmpvp]` | 查看/切换本群数据源（覆盖 WebUI 配置） |
| `/csgo 比分 <队伍>` | 队伍相关比赛 |
| `/csgo 队伍订阅 <队伍>` | 订阅队伍 |
| `/csgo 赛事订阅 <赛事>` | 订阅赛事 |
| `/csgo 队伍取消 <队伍>` | 取消队伍订阅 |
| `/csgo 赛事取消 <赛事>` | 取消赛事订阅 |
| `/csgo 我的订阅` | 显示当前会话订阅；赛事优先展示匹配到的全名 |
| `/csgo 测试 渲染` | 随机抽真实比赛，依次渲染赛前预告 + 每张图结束 + 总数据共 5 张卡 |
| `/csgo 测试 推送` | 向当前会话发一条主动推送，验证路由 |
| `/csgo 测试 API` | 探测列表/详情/实时三个接口 |

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `data_source` | `auto` | 默认数据源下拉（auto 才回退；指定源失败直接报错） |
| `llm_provider_id` | `（空）` | 赛事匹配兜底用的 LLM Provider ID，空则用默认对话模型 |
| `data_source_priority` | `5eplay,wanmei_data,wmpvp` | 数据源优先级，靠前优先 |
| `push_preview` | `true` | 赛前预告推送开关 |
| `preview_minutes` | `15` | 提前多少分钟推赛前预告（窗口内只推一次） |
| `chrome_path` | `（空）` | Chrome/Edge 路径，留空自动查找；仅作图片渲染兜底 |
| `api_base` | `https://gwapi.pwesports.cn` | 网关域名 |
| `match_list_path` | `/eventcenter/app/csgo/event/getMatchList` | 赛程列表 |
| `detail_path` | `/eventcenter/app/csgo/event/getMatchDetail` | 比赛详情 |
| `realtime_path` | `/eventcenter/app/match/getMatchPlayerScoreListRealtime` | 实时比分 |
| `schedule_days` | `1` | 赛程预览天数 |
| `demo_mode` | `false` | 用 Mock 数据响应所有查询 |
| `render_mode` | `auto` | `auto` / `astrbot` / `playwright` / `text` |
| `poll_enable` | `true` | 后台轮询推送总开关 |
| `test_api_cooldown` | `10` | `测试 API` 冷却秒数 |

完整列表见 `_conf_schema.json`。

## 数据卡预览

`docs/preview.png` 是真实赛事（fnatic vs Eternal Fire）渲染出的数据卡：白底、双队徽、大比分、带地图实景背景的逐图比分、5v5 选手数据（KD / ± / ADR / RT）。

队徽、头像、地图背景都在渲染前被抓成 base64 内联，因此不依赖渲染环境的网络与 Referer。

### 地图背景图来源

地图背景**已内置在插件里**，位于 `assets/maps/<map>.webp`，渲染时直接读本地文件转 base64，不联网、不受图床 Referer 限制；即使抽不到真实比赛（回退 Mock），`/csgo 测试 渲染` 也能完整跑通。

图源是 [Counter-Strike Wiki](https://counterstrike.fandom.com/wiki/Counter-Strike_2) 的 CS2 加载图（原图 1920×1080），取 640px 缩略图（单张 36~66KB，10 张合计约 520KB）：

| 地图 | 文件 | 地图 | 文件 |
| --- | --- | --- | --- |
| Anubis | `De_anubis_cs2.png` | Mirage | `De_mirage_cs2.png` |
| Ancient | `De_ancient_cs2.png` | Nuke | `De_nuke_cs2.png` |
| Cache | `De_cache_cs2.png` | Overpass | `Overpass_loading_screen.png` |
| Dust2 | `Cs2_dust2.png` | Train | `De_train_cs2_new.png` |
| Inferno | `Cs2_inferno_remake.png` | Vertigo | `De_vertigo_cs2.jpg` |

回退链（本地图缺失时才走网络）：

```
assets/maps/<map>.webp  →  MAP_IMAGE_WIKI（wiki 缩略图）  →  接口 mapBPDTOS[].mapUrl  →  MAP_IMAGE_FALLBACK  →  无图（浅色行）
```

要换图或加新图，把文件按 `<地图名小写>.webp` 丢进 `assets/maps/` 即可，代码会自动优先使用。

## 推送机制

```text
轮询（按天拉列表）
  → 按 umo 过滤订阅命中
  → 进行中的比赛补一次实时接口（拿 boxNum / mapName）
  → 逐场比对地图状态指纹
     ├─ upcoming → live   : 推送「图X开赛」文字
     ├─ live → finished   : 拉详情 → 渲染数据卡 → 推送（失败退文本）
     └─ 整场 finished     : 推送「比赛结束」总结
```

- **指纹去重**：`(match_id, map_index, map_status, score_a, score_b)`，状态不变不重推。
- **首次基线**：第一次看到某场比赛只记录状态、不推送，避免刚订阅就被历史比赛刷屏。
- **会话隔离**：`send_message(umo, ...)` 精确回到订阅来源会话；同一场比赛被多群订阅时实时接口只请求一次。
- **持久化**：`subs.json`（订阅）与 `state.json`（指纹）落盘，重启不重复推送。
- **临时图清理**：Playwright 产出的图在发送后 120s 删除，另有 900s 兜底清扫。

## 渲染降级

```text
_render_html_to_image()
  ├─ 1. self.html_render(tmpl, data)      AstrBot 内置 T2I（白底高保真）
  ├─ 2. playwright chromium screenshot    #card 元素截图，2x DPR
  └─ 3. None → 调用方输出纯文本卡片（含选手数据）
```

模板：`templates/match_detail.html`（Jinja2），存在即优先使用。

### 为什么第一级要显式传 options

AstrBot 的 t2i 默认参数是 `type=jpeg, quality=40`，视口默认 800px。卡片固定 720 宽，于是会出现两个问题：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 图片发糊 | JPEG quality 40 有损压缩 | `type: "png"` 无损输出 |
| 右侧大片留白 | 视口比卡片宽，`full_page` 按视口宽度截图 | `viewport_width: 720` + 模板里的 `<meta name="viewport" content="width=720">` |
| 高分屏偏软 | 默认 `device_scale_factor` = 1.0 | `device_scale_factor_level: "ultra"`（1.8x，输出 1296px 宽） |

`CARD_WIDTH = 720` 是唯一真源：**模板 CSS 的 `#card` 宽度、`<meta viewport>`、`viewport_width` 三者必须一致**，改宽度时一起改，否则留白会回来。

另外模板里加了 `::-webkit-scrollbar{width:0}`，避免滚动条占掉 15px 布局宽度把卡片挤出画面。

## 自测（仅源码仓库，发布包不含）

发布用的 zip 只保留运行插件所需的文件；下面这些脚本留在源码仓库里，方便二次开发时验证：

```bash
# 插件全链路（含真实接口，50 项断言）
python tests/smoke_test.py

# 独立验证脚本：打印前 3 场赛事与一场详情（不需要 AstrBot）
python tools/probe_api.py
python tools/probe_api.py 2026-10-31

# 3) 两个新数据源独立验证（各打印前 3 场 + 一场详情）
python tools/probe_sources.py
python tools/probe_sources.py 5e
python tools/probe_sources.py wanmei
```

`tests/stubs` 提供 astrbot / httpx 的最小桩实现，仅在测试脚本运行时注入 `sys.path`，不影响 AstrBot 真实运行。

打包发布包：

```bash
python tools/pack.py
```

## 目录结构

```text
astrbot_plugin_wmpvp_csgo/
├── main.py                  # 插件主体（客户端、指令、轮询、渲染）
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── templates/match_detail.html
├── assets/maps/*.webp       # 内置地图背景图（10 张）
├── docs/preview.png
├── tools/                   # 仅源码仓库，不打进发布包
│   ├── pack.py              # 打包脚本
│   ├── probe_api.py         # wmpvp 源验证脚本
│   └── probe_sources.py     # 5EPlay / 完美数据中心验证脚本
├── tests/                   # 仅源码仓库，不打进发布包
│   ├── smoke_test.py
│   └── stubs/               # astrbot / httpx 最小桩
└── data/                    # 运行时生成：subs.json / state.json
```

## 排障

| 现象 | 排查 |
| --- | --- |
| 查询失败 | `/csgo 测试 API` 看三个接口状态；本插件不依赖鉴权，失败多为网络问题 |
| 赛程为空 | 该日期确实无赛事；换 `/csgo 赛程 明天` 或指定日期 |
| 收不到推送 | 确认 `poll_enable=true`；确认该会话有订阅（`/csgo 我的订阅`） |
| 数据卡无队徽 | 检查服务器能否访问 `cdn.wmpvp.com`（需带 Referer，插件已处理） |
| 重复推送 | 检查 `data/state.json` 是否可写（权限问题会导致指纹丢失） |
| 想先看效果 | 打开 `demo_mode`，全部指令用 Mock 数据跑通 |
