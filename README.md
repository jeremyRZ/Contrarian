<p align="center">
  <a href="#contrarian-港股错杀猎手">
    <img src="logo.png" alt="Contrarian Logo" width="400" />
  </a>
</p>

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

</div>

<div align="center">
  <h3>
    <a href="#快速上手">Quick Start</a>
    <span> · </span>
    <a href="#主要功能">Features</a>
    <span> · </span>
    <a href="#api-端点索引">API</a>
    <span> · </span>
    <a href="#项目结构">Structure</a>
  </h3>
</div>

---

## Why Contrarian?

港股市场里，好公司跌了——是基本面恶化，还是情绪错杀？

大多数投研工具告诉你「这只股票在跌」。Contrarian 告诉你**为什么它可能被错杀了**。

我们把南向资金、公司回购、新闻情绪、资金流向、估值分位、机构增减持、股息率、财报窗口等 **8 个维度的研究数据**，折算成一套量化的**反向信号评分模型**（0~10 分）。当一只股票技术面超跌 + 反向信号强烈时，它就是被市场错杀的候选。

**核心原则：**
- **本地运行，数据不出本机** — 除 FutuOpenD 外不上传任何持仓或交易信息
- **优雅降级** — 任一数据源挂掉不影响整体评分，UI 不崩溃
- **信号驱动，不是直觉** — 每一档加分都有明确的数据来源和权重

详细技术文档见 [references/README.md](references/README.md)。

---

## 主要功能

| 页面 | 说明 |
|------|------|
| 今日决策 | 只展示已通过研究门槛的日线策略动作、建议股数、市场过滤和验证指标 |
| 个股 | 查看技术面、反向证据、主要风险，并设置观察池、自选股和价格报警 |
| 风险控制 | 汇总实际持仓的止损/止盈建议与价格报警状态 |

旧六策略、盘中抄底和 IPO 页面已退出生产流程。相关模块仅保留作离线研究，不会由网站或后台调度器自动产生买入推送。

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

## 快速上手

### 前置条件

1. 安装并运行 **FutuOpenD**（[下载](https://www.futunn.com/download/OpenAPI)），网关监听 `127.0.0.1:11111`
2. 富途证券账户（真实或模拟）；交易/持仓类功能需填 `acc_id`
3. Python 3.13+

### 安装与启动

```bash
# 克隆仓库
git clone https://github.com/<your-user>/Contrarian.git
cd Contrarian

# 安装依赖
pip install -r requirements.txt

# 配置
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入 futu.host/port/trd_env/acc_id

# 启动
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000
```

浏览器打开 **http://localhost:8000**

> 富途未连接时，页面顶部显示「富途未连接」，数据接口快速返回错误提示（不挂起），UI 不崩溃。

### 配置说明

```yaml
futu:
  host: "127.0.0.1"   # FutuOpenD 网关地址
  port: 11111          # FutuOpenD 监听端口
  trd_env: "REAL"      # REAL（真实）或 SIMULATE（模拟）
  acc_id: ""           # 富途 App 内查看，留空取首个账户

news:
  llm:                 # 可选：新闻情绪 LLM 复核（未配置则零成本降级词典法）
    base_url: ""       # OpenAI 兼容接口地址
    api_key: ""
    model: ""

monitor:
  holdings_exclude: ["HK.44165"]  # 手动排除列表（补充自动剔除）

# 每日持仓资金面背离报告推送（企业微信群机器人 + 后端内置调度器）
wecom:
  webhook: ""          # 企业微信群机器人 Webhook；留空则推送降级为日志
schedule:
  enabled: true        # 每日自动推送开关
  time: "16:30"        # 推送时间（本地时间，建议收盘后）

# 盘中「恒科急跌联动」低吸扫描调度
intraday:
  enabled: true            # 盘中调度总开关
  interval_min: 30         # 扫描间隔（分钟）
  start: "09:30"           # 交易时段开始（本地时间 HK）
  end: "16:00"             # 交易时段结束
  threshold: -2.0          # 急跌阈值（恒科涨跌%，≤ 此值判定为急跌）
  hstech_code: "HK.800700" # 恒生科技指数（注意：HK.800000 是恒生指数，非科技指数）
```

---

## 项目结构

```
Contrarian/
├── logo.png                  # 项目 Logo
├── LICENSE                   # MIT 许可证
├── README.md                 # 本文件 — 项目概览
├── config.example.yaml       # 配置模板
├── requirements.txt          # Python 依赖
│
├── app/
│   ├── api.py                # FastAPI 路由（所有 HTTP 端点）
│   ├── futu_client.py        # 富途 OpenAPI 封装
│   ├── cache.py              # 5 分钟 TTL 缓存层
│   ├── notify.py             # 企业微信推送（指纹去重）
│   ├── intraday_scheduler.py # 盘中扫描调度器（交易时段守护线程）
│   └── modules/
│       ├── intraday.py         # ★ 恒科急跌联动低吸扫描（指数判定+反向信号加权）
│       ├── reverse_signals.py  # ★ 八档反向信号聚合（核心模型）
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
│       └── ipo.py              # 新股打新打分
│
├── frontend/
│   ├── index.html            # 跳转到今日决策
│   ├── strategy-center.html  # 已通过准入的策略动作
│   ├── analyze.html          # 个股研究
│   ├── risk.html             # 持仓和价格风控
│   ├── app.css               # Apple 风共享样式
│   └── app.js                # 通用工具 / 导航 / 下钻
│
├── references/
│   └── README.md             # 详细技术文档（接口表 + 模块权重细节）
│
└── watchlist.json            # 本地观察池（跨浏览器持久化）
```

---

## API 端点索引

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 富途连接健康检查 |
| GET | `/strategy-center/status` | 已通过研究门槛的策略动作（只读） |
| GET | `/analyze?code=HK.00700` | 单票技术面 + 八档反向信号 |
| GET | `/analyze/full?code=HK.00700` | 单票聚合分析：技术面、反向信号源数据与决策一次返回（前端主入口） |
| GET | `/monitor` | 持仓风控（自动过滤窝轮/杠杆ETF） |
| GET | `/daily-divergence` | 只读生成每日持仓资金面背离报告 |
| POST | `/daily-divergence/push` | 生成报告并显式推送企业微信 |
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
| GET | `/futu-watchlist` | 富途自选股（用户自建分组，可增删；`group` 可覆盖目标分组） |
| POST | `/futu-watchlist` | 富途自选股增删：body `{code, name?, action?: 'add'\|'remove', group?}` |

完整字段说明与信号权重细节见 **[references/README.md](references/README.md)**。

---

## 设计与实现要点

- **优雅降级**：所有外部数据调用失败都计 0 或返回友好错误，绝不挂起 UI / 拖垮整体评分
- **自动剔除噪声**：`filters.is_tradable` 用快照判定停牌 / `last_price<=0` / `pe&pb<=0`，持仓与候选池自动过滤（停牌老千股不再需要手动维护排除列表）
- **衍生品清洗**：持仓监控跳过窝轮/杠杆 ETF 的价格/盈亏计算，避免垃圾数值
- **可选 LLM 复核**：配置 `config.yaml` 的 `news.llm` 后，新闻情绪自动走模型复核；未配置时零成本降级到词典法
- **推送去重**：企业微信推送用指纹（内容哈希）去重，升级才推、不重复轰炸

---

## 已知限制

| 限制 | 说明 |
|------|------|
| 富途自选股增删 | 仅对用户自建分组有效（`futu.watchlist_group`，默认 `Contrarian`）；系统分组「全部」不可写。需先在富途客户端创建同名用户分组，API 无法创建分组。「★自选」按钮可点击增删 |
| 财报日历 | 东方财富港股业绩日历常返回空；财报窗口以「月份启发式」为主信号（恒可用），精确窗口仅作增强 |
| 本地部署 | 默认绑定 `127.0.0.1`，仅本机访问；远程需自行加反向代理与鉴权 |
| 非投资建议 | 所有评分与信号仅为个人研究辅助，不构成任何买卖建议 |

---

## License

[MIT](LICENSE) — 详见 [LICENSE](LICENSE) 文件。

---

<p align="center">
  Built with focus on what matters.
</p>
