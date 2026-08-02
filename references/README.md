# 港股投资研究平台 · Contrarian 港股错杀猎手

基于 **富途 OpenD (FutuOpenD)** 的港股投研 Web 平台（个人投资研究用）。

前端为 **Apple 风多页面结构**（顶部横向导航、无图标、纯文字），每个功能对应独立 HTML 页面，便于日后各自扩展 JS：
- **index.html — 错杀猎手**（核心）：合并原「选股扫描」+「错杀猎手」，内设内部分栏「综合选股扫描 / 错杀观察池」。
- **analyze.html — 单票深度**（核心）：原「单票分析」，作为二级下钻——在错杀猎手列表点击任意股票代码/名称即跳转 `analyze.html?code=` 并自动分析，也可直接输入代码查询。单票深度页集成三块研究数据：**南向资金**（港股通净买入 + 个股持股）、**回购 Timeline**、**相关新闻**，三者并行拉取、各自优雅降级。
- **risk.html — 风控与预警**（核心）：合并原「持仓风控」+「价格报警」，含持仓监控、价格报警检查与新增报警。
- **ipo.html — 新股打新**（辅助）：港股打新专用，边缘工具，置于导航末尾不抢占主功能注意力。

共享资源：`app.css`（Apple 风样式）、`app.js`（通用工具、连接检测、下钻跳转、导航激活态）。

## 一、前置条件
1. 安装并运行 **FutuOpenD**（https://www.futunn.com/download/OpenAPI），监听 `127.0.0.1:11111`
2. 富途证券账户（真实或模拟）；交易类功能需填 `acc_id`
3. Python 3.13（已用 managed 环境，依赖见 requirements.txt）

## 二、安装
```bash
# 使用隔离虚拟环境（已建好）
PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
PIP=~/.workbuddy/binaries/python/envs/default/Scripts/pip.exe
$PIP install -r requirements.txt
```

## 三、配置
复制 `config.example.yaml` 为 `config.yaml`，修改：
```yaml
futu:
  host: "127.0.0.1"
  port: 11111
  trd_env: "REAL"      # 或 SIMULATE
  acc_id: "你的账户ID"  # 在富途 App 内查看，留空取首个账户
```

## 四、启动
```bash
cd hk-stock-platform
PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
$PY -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```
浏览器打开 http://localhost:8000

> 富途未连接时，页面顶部显示「富途未连接」，数据接口快速返回错误提示（不挂起），UI 不崩溃。

## 五、接口
| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | /health | 连接健康检查 |
| GET  | /valuation?code=HK.00700 | 估值分析（可带 financials JSON） |
| GET  | /screener?top_n=20&codes= | 选股扫描（6 策略 + 仓位感知） |
| GET  | /missed-scan?top_n=5&pool=leaders | 错杀观察扫描（Contrarian 核心） |
| GET  | /monitor | 持仓风控（止损/止盈/技术面 + 企业微信推送） |
| GET  | /price-alerts | 价格报警检查；POST 增运行时报警 |
| GET  | /analyze?code=HK.00700 | 单票实时技术面（MA/RSI/量能） |
| GET  | /buybacks?code=HK.00700 | 个股回购记录（富途 get_corporate_actions_buybacks） |
| GET  | /news?code=HK.00700 | 个股相关新闻（富途 get_search_news，关键词=代码/名称） |
| GET  | /southbound?code=HK.00700 | 个股港股通持股（东方财富 `RPT_MUTUAL_STOCK_HOLDRANKS`，实时，含持股数/占比/当日增减/连续增持减持天数与反向风险）。**不带 code 时**返回全市场港股通每日净买额（`RPT_MUTUAL_DEAL_HISTORY` MUTUAL_TYPE=006，市场概览用，不进个股页避免误导） |
| GET  | /capital-flow?code=HK.00700&days=20 | 个股资金流向（大单/超大单）：`distribution` 逐档(超大/大/中/小单)净流入快照（富途 `get_capital_distribution`）+ `flow` 每日净流入序列（富途 `get_capital_flow`，含主力=超大+大、各档近 N 日汇总） |
| GET  | /southbound-risk | 南向减持风险聚合预警：扫持仓（无持仓退回龙头池），汇总存在连续减持/骤减反向风险的标的，供风控页统一预警 |
| GET  | /fundamentals?code=HK.00700 | 个股基本面反向信号源数据：估值分位（PE/PB 历史分位 + 行业排名，富途 `get_valuation_detail`）+ 机构增减持（增持/减持两向汇总净方向 + 共识度，富途 `get_shareholders_holding_changes`）。单票深度页「基本面反向信号」卡片展示。 |
| GET  | /dividend?code=HK.00700 | 个股股息率/分红反向信号（第 7 档源数据）：TTM 股息率 + 增派/弃派判定 + 评分（富途快照 `dividend_ratio_ttm`）。 |
| GET  | /earnings?code=HK.00700 | 个股财报窗口信号（第 8 档源数据）：财报季月份启发式 + 可选东财业绩日历精确窗口 + 评分。 |
| GET  | /holdings | 持仓中的正股（排除窝轮/杠杆ETF **且自动剔除停牌/无报价/无估值标的**），供单票页快速选择。轻量，不做技术面。可用 `config.yaml` 的 `monitor.holdings_exclude` 列表补充排除。 |
| GET  | /watchlist | 读取用户观察池（存 `watchlist.json`，跨浏览器持久化）。 |
| POST | /watchlist | 加入/移除观察池：body `{code, name?, action?: 'add'|'remove'}`。 |
| POST | /ipo | 打新打分（JSON body） |
| GET  | /ipo/meta | 打分权重与可选项 |
| GET  | /ipo/auto?code=HK.02197 | 联网获取招股信息并自动打分（失败降级手动输入） |

## 六、模块说明
- **选股扫描**：龙头观察池快照 + 6 大策略信号 + 10 分综合评分；仓位感知（轻仓6/中仓7/满仓停推）
- **持仓风控**：富途持仓按品种止损/阶梯止盈 + 技术面止盈（放量滞涨/长上影/RSI超买）+ 企业微信推送（指纹去重）
- **错杀猎手**：筛选「盈利+被错杀」优质标的，输出 TopN 确信度，列表指纹去重推送
- **价格报警**：预警→警告→止损 三级 + 止盈目标 + 日内异常跌幅；升级才推、不重复轰炸
- **单票分析**：富途快照 + K 线算 MA5/10/20、RSI14、量能比 + 技术面结论
- **南向 / 回购 / 新闻（单票深度增强）**：市场级南向每日净买额走东方财富 `RPT_MUTUAL_DEAL_HISTORY`（MUTUAL_TYPE=006，与官网沪深港通资金流向同源，**直接调用、不依赖 akshare**，已删除旧的 push2his 额度余额假兜底）；个股港股通持股走东方财富 `RPT_MUTUAL_STOCK_HOLDRANKS` **实时可用**，含持股数/占发行比/日变动/连续增持天数，以及反向风险字段（连续减持≥3日、单日骤减≤−5%）；回购与新闻走富途 OpenAPI（新闻用 `get_search_news`，非旧版 `get_news`）。三者并行拉取、各自优雅降级，互不阻塞。
- **反向信号评分（reverse_signals，v1.3.0 新增，v1.3.1 增强，v1.4.0 接入资金流向，v1.5.0 接入估值分位 + 机构增减持，v1.5.2 接入机构共识度加权 + 批量扫描，v1.6.0 接入股息率 + 财报窗口 + 新闻情绪 LLM 复核 + 自动剔除停牌/无报价标的，v1.6.1 移除沽空第7档（数据滞后严重））**：将八块研究数据折算为「错杀反向信号」（clamp 到 [-1.0, 10.0]），注入错杀猎手、综合选股与单票页。权重（代码为准）：
  - 南向个股持股（smart money）：连续增持 ≥5日 +2.5 / ≥3日 +2.0 / 2日 +1.0 / 1日(当日净增持) +0.5；当日增持力度 chg_ratio_1d≥0.3% 额外 +0.5。**反向风险扣分**：连续减持 ≥5日 −1.5 / ≥3日 −1.0；单日骤减 chg_ratio_1d≤−5% 额外 −1.0（标「南向连续减持N日(风险)」「单日南向骤减X%(风险)」，进风险信号）。
  - 公司回购：近30日有回购 +2.0 / 近60日 +1.0 / 更早历史 +0.3；近30日≥3次回购额外 +0.5（标「连续多日回购」）。
  - 新闻情绪（细粒度打分，v1.3.1 升级）：`news.py` 对每条标题做中文金融情绪打分（词典 + 否定词翻转「不/未/解除/终止…」+ 程度词放大），输出 `sentiment`（强烈利好/利好/中性/利空/强烈利空）与 `sentiment_score`(−3~3)；`reverse_signals` 按近度加权聚合为净情绪，映射为 +1.5/ +0.75 / 0 / −0.5 / −1.0（替代旧版 pos−neg 粗分类）。
  - 资金流向（机构主力 = 超大单 + 大单净流入，富途逐档，v1.4.0 接入）：取近 N 日主力净流入汇总为主信号（趋势更稳）、最新时段快照为辅。净流入 ≥10亿 +2.0 / ≥1亿 +1.5 / ≥1000万 +1.0 / >0 +0.5；**主力占比(main_ratio)≥60%（机构主导）额外 +0.3**；净流出（机构在撤退，错杀反向利空）≥1亿 −1.0 / <0 −0.5（标「主力大额净流出(风险)」「主力净流出(风险)」）。
  - 估值分位（错杀核心，v1.5.0 接入）：富途 `get_valuation_detail` 读 PE/PB 历史分位与行业排名。PE 历史分位 ≤20% +2.0 / ≤40% +1.0 / ≤60% +0.3 / >80% −0.5；PB 历史分位 ≤20% 额外 +0.3；行业排名前 1/3 额外 +0.5（标「PE 历史分位极低/偏低」「行业内估值偏低」）。
  - 机构增减持（smart money，v1.5.0 接入，v1.5.2 加共识度加权）：富途 `get_shareholders_holding_changes` 分增持/减持两向汇总近 N 期净方向，并据增/减机构数判定共识度。多家(≥3)一致净增持 +2.0 / ≥2期净增持 +1.5 / 净增持 +0.5；多家(≥3)一致净减持 −1.5 / ≥2期净减持 −1.0 / 净减持 −0.5（标「多家机构一致增持(强共识)」「多家机构一致减持(强风险)」等，details 附 `consensus`）。
  - 沽空反向信号（第 7 档，v1.5.2 接入，**v1.6.1 移除**）：东方财富港股沽空记录页数据滞后数周（非实时），已移除该档。`/short-sell` 端点同步删除。
  - 股息率 / 分红（第 7 档，v1.6.0 接入）：富途 `get_market_snapshot` 自带 TTM 股息字段（`dividend_ratio_ttm` 股息率、`dividend_ttm` 每股股息、`dividend_lfy` 上一财年股息），无需另调 `get_dividend_info`（该 futu 版本无此方法）。港股高股息错杀核心维度：yield ≥8% +1.5 / ≥5% +1.0 / ≥3% +0.5 / 0<yield<3% +0.2；增派(ttm>lfy×1.1) 额外 +0.3；弃派/削减(ttm=0 但 lfy>0) −1.0(分红断裂风险)。新增 `/dividend` 端点与单票页「股息」分解段。
  - 财报窗口期（第 8 档，v1.6.0 接入）：业绩披露前后的错杀机会窗口。主信号为**财报季月份启发式**（年报季 3~4 月、中报季 8~9 月 → +0.2「财报季」），辅信号为**可选东财业绩日历**（`RPT_LICO_FN_CPD`，港股常返回空，仅作增强）取精确下次业绩日，±14 日内 → +0.5「财报窗口·错杀机会」。无数据则计 0（不计入）。新增 `/earnings` 端点与单票页「财报窗口」分解段。
  - **新闻情绪升级（v1.6.0）**：词典法扩充否定/澄清词（辟谣/否认/澄清/传闻不实）改善反讽与辟谣标题识别；并新增**可选轻量 LLM 复核**——若 `config.yaml` 的 `news.llm` 配置了 `base_url`+`api_key`（OpenAI 兼容接口），则对每条标题调用 LLM 做情绪复核，失败/未配置时自动降级到词典法（默认行为，零成本）。
  - **自动剔除停牌 / 无报价标的（v1.6.0）**：`filters.is_tradable` 用富途快照判断 `suspension==True`、`last_price<=0`、`pe_ratio<=0 且 pb_ratio<=0` 任一即视为不可交易。持仓快捷选择 `/holdings`、综合选股 `/screener`、错杀扫描 `missed_scan`（复用 screener 基础分）自动过滤此类标的，免去手动维护排除列表（原 `monitor.holdings_exclude` 仍作为补充保留）。
  - `missed-scan` 输出 `reverse`/`reverse_signals`/`conviction`（确信度 = 基础分 + 超跌加分 2 + 反向加分），按 conviction 排序；`/screener` 输出 `reverse`/`reverse_signals`/`total_score`（= 基础分 + 反向加分），按 total_score 排序；`/analyze` 附 `reverse` 字段。推送门槛纳入总分：基础分达标 **或** 反向强化后总分达标均触发买入机会通知（v1.4.0 调整），使「技术面略低但南向/回购/新闻/资金流向/估值/机构/股息/财报强烈反向看好」的错杀候选也能进入推送。为控 API 量，仅对基础分靠前的候选拉取八块数据（missed-scan Top12 / screener Top15）；v1.5.2 起该批候选改用 `reverse_score_batch` 并行预拉取南向+估值、逐票并发计算，显著降低墙钟延迟。
- **资金流向（capital_flow，v1.4.0 新增）**：富途 OpenAPI 逐档统计个股大单/超大单资金流向。`distribution`（最近交易时段，单位港元）给出 超大单/大单/中单/小单 的流入、流出与净流入，并汇总 `main_net`(主力=超大+大) 与 `total_net`(全部档合计)；`flow` 给出每日 `super/big/mid/small/main/total` 净流入序列与近 N 日窗口汇总。单票深度页新增「资金流向（大单/超大单）」卡片展示，红涨绿跌（港股习惯）。
- **南向减持风险聚合（southbound_risk，v1.4.0 新增）**：扫描持仓（已配置交易账户）或龙头观察池，对每只标的新查港股通持股并汇总含 `risk` 的标的（连续减持/骤减反向风险），风控页「南向减持风险预警」卡片统一展示。
- **新股打新**（港股打新专用 skill 移植）：5 维打分（市场情绪30%/基本面20%/稀缺性23%/估值12%/配售15）+ 一句话说明 + 申购建议；中签率预测具名档位（甲头/甲中/甲大/甲尾/大甲尾；乙头/乙中/乙尾/顶头锤）+ 5 个真实校准案例（蜜雪/茶百道/古茗/巨星传奇/布鲁可）；招股信息完整字段（核心数据速览）；联网获取（新浪 best-effort，失败降级）
