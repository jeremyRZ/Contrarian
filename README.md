<p align="center"><img src="frontend/logo.png" alt="Contrarian Logo" width="400"></p>

# Contrarian

只读的多市场投资研究与港股策略监控系统。正式港股信号只来自策略中心；系统可以读取富途行情、资金与持仓并生成模拟建议，但没有真实下单接口。

## 一键启动

1. 启动 Futu OpenD，确认 API 端口为 `127.0.0.1:11111`。
2. 双击桌面上的 Contrarian 启动入口，或在项目目录运行 `start_contrarian.ps1`。
3. 浏览器打开 `http://127.0.0.1:8000/strategy-center.html`。

默认 Python 环境为 `G:\Coding\envs\ContestTrade\python.exe`。配置放在 `config.yaml`；企业微信 Webhook 等凭证不要提交到 Git。

一键启动会同时启动单实例 Watchdog；`install_watchdog_startup.ps1` 可额外注册当前用户登录自启动。Watchdog 每5分钟检查网站与 OpenD、补跑遗漏的收盘任务并重试企业微信 outbox，状态写入 `.runtime/watchdog.json`。如需覆盖电脑关机或守护进程死亡，在本机 `config.yaml` 的 `watchdog.heartbeat_url`（或环境变量 `CONTRARIAN_HEARTBEAT_URL`）填写外部 dead-man 服务地址；未配置外部地址时，本机进程死亡无法自行报警。

## 正式港股策略

- `xiaomi_trend_v1`：小米日线 MA20/MA60 趋势，50% 资金上限，整手执行。
- `hk_liquid_trend_rotation_v2`：动态流动性池、恒指 MA200 门控、200 日风险调整动量、每 20 个交易日复核。
- `hk_long_term_high_breakout_v1`：120 日放量新高突破，ATR14×2 初始止损。

运行参数、仓位和历史验证指标的唯一契约位于 `strategies/`。页面和后端直接读取这些 YAML，不再维护第二套硬编码参数。

## 关键入口

- `/strategy-center/status`：唯一正式策略状态、动作队列、资金和持仓约束。
- `/forward-ledger`：信号后的前向表现与影子成交。
- `/analyze/full`：个股事实与反向证据，不生成另一套交易结论。
- `/api/markets`、`/api/positions`：多市场行情与持仓视图。
- `/intraday/status`：盘中提醒与持仓风险检查状态；不运行已否决的分钟级策略。

## 开发与验证

```powershell
G:\Coding\envs\ContestTrade\python.exe -m pytest -q
G:\Coding\envs\ContestTrade\python.exe -m compileall -q app scripts
```

核心目录：

```text
app/                 FastAPI 服务、市场适配与分析模块
frontend/            投资台、策略中心、持仓风险与验证页面
strategies/          三套正式策略的 YAML 契约
scripts/             数据更新、候选研究与逐日走查脚本
tests/               回归、边界与策略行为测试
```

历史研究结论见 `*_RESEARCH*.md` 与 `INTRADAY_RESEARCH_STATUS.md`。被否决的研究代码不留在生产树中，结论文档保留用于避免重复试错。
