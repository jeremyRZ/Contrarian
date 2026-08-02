# Contrarian 港股错杀猎手

> 基于 **富途 OpenD（FutuOpenD）** 的港股个人投研 Web 平台。把「南向资金、公司回购、新闻情绪、资金流向、估值分位、机构增减持、股息率、财报窗口」等研究数据，折算成一套 **「错杀反向信号」评分模型**，帮你在下跌中识别被市场错杀的优质标的。

版本：**v1.6.1**　|　技术栈：FastAPI + uvicorn（后端）／原生 HTML + CSS + JS + ECharts（前端）／富途 OpenAPI（数据源）

---

## 一、这是什么

一个**本地运行**的港股研究工具（个人投资研究用途，非投资建议）。核心思路是 *Contrarian*（逆向）——当一只股票基本面良好却在下跌时，往往是被情绪错杀的买入机会。平台把所有能拿到的「聪明钱信号」量化成一个 0~10 分的反向信号分，叠加技术面超跌分，输出一份按确信度排序的「错杀候选」清单。

- **数据源**：富途 OpenAPI（行情/持仓/回购/新闻/资金流向/估值/机构），东方财富（港股通持股、财报日历辅助）
- **运行方式**：后端 FastAPI 跑在 `127.0.0.1:8000`，前端是几个静态 HTML 页面，浏览器打开即用
- **依赖**：除 FutuOpenD 外，全部本地化，不上传任何持仓/交易数据

---

## 二、主要功能

| 页面 | 路径 | 说明 |
|------|------|------|
| 错杀猎手 | `frontend/index.html` | 核心。综合选股扫描 + 错杀观察池，输出 TopN 错杀候选（含反向信号分与确信度），支持列表指纹去重推送 |
| 单票深度 | `frontend/analyze.html` | 核心。输入/点击任意股票代码，下钻分析技术面（MA/RSI/量能）+ 八档反向信号分解图 + 南向/回购/新闻三卡片 + 观察池/预警/自选股操作 |
| 风控与预警 | `frontend/risk.html` | 持仓风控（止损/止盈/技术面）+ 价格报警（预警→警告→止损三级 + 日内异常跌幅）与企业微信推送 |
| 新股打新 | `frontend/ipo.html` | 港股打新 5 维打分 + 中签率预测 + 招股信息速览（辅助工具） |

### 反向信号评分模型（八档，clamp 到 `[-1.0, 10.0]`）

| # | 维度 | 正向加分要点 |
|---|------|------|
| 1 | 南向持股 | 港股通连续增持（smart money 在买）+0.5~+2.5 |
| 2 | 公司回购 | 近 30 日回购 +2.0，多日回购额外 +0.5 |
| 3 | 新闻情绪 | 词典法 + 可选 LLM 复核，近度加权聚合 +0.75~+1.5 |
| 4 | 资金流向 | 机构主力（超大+大单）净流入 +0.5~+2.0，机构主导额外 +0.3 |
| 5 | 估值分位 | PE/PB 历史分位极低 +0.3~+2.0，行业低估额外 +0.5 |
| 6 | 机构增减持 | 多家一致净增持（强共识）+0.5~+2.0 |
| 7 | 股息率/分红 | TTM 股息率 ≥3%/5%/8% 分档 +0.5~+1.5，增派额外 +0.3 |
| 8 | 财报窗口 | 财报季月份 +0.2，精确窗口 ±14 日 +0.5 |

> 注：原第 7 档「沽空」因东方财富数据源滞后数周（非实时），已于 v1.6.1 移除。

---

## 三、快速上手

### 前置条件
1. 安装并运行 **FutuOpenD**（[下载](https://www.futunn.com/download/OpenAPI)），网关监听 `127.0.0.1:11111`
2. 富途证券账户（真实或模拟）；交易/持仓类功能需填 `acc_id`
3. Python 3.13（项目使用隔离 venv，依赖见 `requirements.txt`）

### 安装依赖
```bash
cd Contrarian
PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
PIP=~/.workbuddy/binaries/python/envs/default/Scripts/pip.exe
$PIP install -r requirements.txt
```

### 配置
复制模板并按需修改：
```bash
cp config.example.yaml config.yaml
```
```yaml
futu:
  host: "127.0.0.1"
  port: 11111
  trd_env: "REAL"      # 或 SIMULATE
  acc_id: ""           # 富途 App 内查看，留空取首个账户
```

### 启动
```bash
PY=~/.workbuddy/binaries/python/envs/default/Scripts/python.exe
$PY -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```
浏览器打开 **http://localhost:8000**

> 富途未连接时，页面顶部显示「富途未连接」，数据接口快速返回错误提示（不挂起），UI 不崩溃。

---

## 四、项目结构

```
Contrarian/
├── app/
│   ├── api.py              # FastAPI 路由（所有 HTTP 端点）
│   ├── futu_client.py      # 富途 OpenAPI 封装（行情/交易/自选股）
│   ├── cache.py            # 5 分钟 TTL 缓存层（@cached）
│   ├── notify.py           # 企业微信推送（指纹去重）
│   └── modules/
│       ├── reverse_signals.py  # 八档反向信号聚合（核心模型）
│       ├── screener.py         # 综合选股扫描（6 策略）
│       ├── missed_scan.py      # 错杀观察扫描（Contrarian 核心）
│       ├── monitor.py          # 持仓风控（过滤窝轮/杠杆ETF）
│       ├── price_alert.py      # 价格报警
│       ├── southbound.py       # 港股通持股（东财）
│       ├── capital_flow.py     # 资金流向（富途逐档）
│       ├── fundamentals.py     # 估值分位 + 机构增减持
│       ├── news.py             # 新闻情绪（词典 + 可选 LLM 复核）
│       ├── dividend.py         # 股息率/分红信号
│       ├── earnings.py         # 财报窗口信号
│       ├── filters.py          # 停牌/无报价自动剔除
│       ├── buybacks.py         # 公司回购
│       ├── valuation.py        # 估值分析
│       ├── ipo.py              # 新股打新打分
│       └── ...
├── frontend/
│   ├── index.html          # 错杀猎手
│   ├── analyze.html        # 单票深度
│   ├── risk.html           # 风控与预警
│   ├── ipo.html            # 新股打新
│   ├── app.css             # Apple 风共享样式
│   └── app.js             # 通用工具/导航/下钻
├── references/
│   └── README.md           # 详细技术文档（接口表 + 模块权重细节）
├── config.example.yaml     # 配置模板
├── requirements.txt
└── watchlist.json          # 本地观察池（跨浏览器持久化）
```

---

## 五、API 端点索引

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 富途连接健康检查 |
| GET | `/screener` | 综合选股扫描（6 策略 + 仓位感知 + 自动剔除停牌） |
| GET | `/missed-scan` | 错杀观察扫描（核心） |
| GET | `/analyze?code=HK.00700` | 单票技术面 + 八档反向信号 |
| GET | `/monitor` | 持仓风控（自动过滤窝轮/杠杆ETF） |
| GET/POST | `/price-alerts` | 价格报警检查 / 新增报警 |
| GET | `/southbound` | 港股通持股（个股或全市场净买额） |
| GET | `/capital-flow` | 资金流向（大单/超大单） |
| GET | `/fundamentals` | 估值分位 + 机构增减持 |
| GET | `/news` | 个股相关新闻情绪 |
| GET | `/buybacks` | 公司回购记录 |
| GET | `/dividend` | 股息率/分红信号 |
| GET | `/earnings` | 财报窗口信号 |
| GET | `/holdings` | 持仓正股（已自动剔除停牌/无报价） |
| GET/POST | `/watchlist` | 本地观察池读取 / 增删 |
| GET | `/futu-watchlist` | 富途自选股（只读，API 不支持写系统分组） |
| GET/POST | `/ipo` · `/ipo/meta` · `/ipo/auto` | 新股打新 |

完整字段说明与信号权重细节见 **[references/README.md](references/README.md)**。

---

## 六、设计与实现要点

- **优雅降级**：所有外部数据调用失败都计 0 或返回友好错误，绝不挂起 UI / 拖垮整体评分
- **自动剔除噪声**：`filters.is_tradable` 用快照判定停牌 / `last_price<=0` / `pe&pb<=0`，持仓与候选池自动过滤（中国绿宝这类停牌老千股不再需要手动维护排除列表）
- **衍生品清洗**：持仓监控 `monitor` 跳过窝轮/杠杆 ETF 的价格/盈亏计算，避免垃圾数值
- **可选 LLM 复核**：在 `config.yaml` 的 `news.llm` 配置 OpenAI 兼容接口后，新闻情绪自动走模型复核；未配置时零成本降级到词典法
- **推送去重**：企业微信推送用指纹（内容哈希）去重，升级才推、不重复轰炸

---

## 七、已知限制

- **富途自选股只读**：FutuOpenD 的所有自选分组均为 SYSTEM 类型，OpenAPI 不支持增删，故「★自选」按钮仅显示状态；增删请在富途客户端操作
- **财报日历**：东方财富港股业绩日历在沙箱环境常返回空，财报窗口信号以「财报季月份启发式」为主信号（恒可用），精确窗口仅作可选增强
- **本地部署**：服务默认绑定 `127.0.0.1`，仅本机访问；如需远程请自行加反向代理与鉴权
- **非投资建议**：所有评分与信号仅为个人研究辅助，不构成任何买卖建议

---

## 八、许可与免责

个人研究项目，自行承担使用风险。市场有风险，投资需谨慎。
