# 多市场架构与A股接入

当前版本保留原有港股生产链路，并完成前三阶段的多市场扩展。A股功能默认只读；美股的证券模型、币种、基准、交易时段和接口位置已经预留，但数据与策略尚未启用。

## 已实现

- 统一证券代码：`HK.00700`、`SH.603993`、`SZ.000001`、`US.AAPL`。
- 市场规则：币种、基准、交易时段、手数、结算、佣金和印花税。
- 数据路由：Futu实时数据优先；长周期研究可以复用`.runtime/<code>_qfq_daily.csv`。
- A股只读接口：搜索、快照、K线、分析、分市场持仓、研究候选池。
- A股执行回测：下一交易日开盘成交、100股整数手、T+1、涨跌停不可成交、佣金最低收费和卖出印花税。
- 验证门禁：训练60%、验证20%、盲测20%；历史通过后只进入`PAPER`，不会直接变成真实交易信号。
- ContestTrade桥接：导入的事件候选固定标记为`RESEARCH_ONLY`。
- 网站入口：`/markets.html`。

## API

```text
GET  /api/markets
GET  /api/securities/search?market=CN&q=洛阳
GET  /api/securities/SH.603993/snapshot
GET  /api/securities/SH.603993/bars?count=600
GET  /api/securities/SH.603993/analysis
GET  /api/positions?market=CN
GET  /api/cn/candidates?codes=SH.603993,SZ.000001
GET  /api/cn/backtest/SH.603993
GET  /api/cn/events
POST /api/cn/events/import
```

ContestTrade导入格式：

```json
{
  "items": [
    {
      "code": "SH.603993",
      "name": "洛阳钼业",
      "score": 7.2,
      "catalysts": ["公告或行业催化"],
      "risks": ["周期价格回落"]
    }
  ]
}
```

## A股账户

系统会在对应市场的富途交易上下文中自动发现与`trd_env`匹配的账户。通常不需要填写账户ID；只有同一市场存在多个同类账户时，才建议显式指定：

```yaml
futu:
  accounts: {HK: "", CN: "", US: ""}
```

行情或账户权限不足时，接口明确返回不可用，不会用零值伪装成真实数据。
