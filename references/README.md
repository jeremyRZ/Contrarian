# 港股投资研究平台 · Contrarian 港股错杀猎手

基于 **富途 OpenD (FutuOpenD)** 的港股投研 Web 平台（个人投资研究用）。
前端 6 个 Tab：选股扫描 · 持仓风控 · 错杀猎手 · 价格报警 · 单票分析 · 新股打新。

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
| POST | /ipo | 打新打分（JSON body） |
| GET  | /ipo/meta | 打分权重与可选项 |
| GET  | /ipo/auto?code=HK.02197 | 联网获取招股信息并自动打分（失败降级手动输入） |

## 六、模块说明
- **选股扫描**：龙头观察池快照 + 6 大策略信号 + 10 分综合评分；仓位感知（轻仓6/中仓7/满仓停推）
- **持仓风控**：富途持仓按品种止损/阶梯止盈 + 技术面止盈（放量滞涨/长上影/RSI超买）+ 企业微信推送（指纹去重）
- **错杀猎手**：筛选「盈利+被错杀」优质标的，输出 TopN 确信度，列表指纹去重推送
- **价格报警**：预警→警告→止损 三级 + 止盈目标 + 日内异常跌幅；升级才推、不重复轰炸
- **单票分析**：富途快照 + K 线算 MA5/10/20、RSI14、量能比 + 技术面结论
- **新股打新**（港股打新专用 skill 移植）：5 维打分（市场情绪30%/基本面20%/稀缺性23%/估值12%/配售15）+ 一句话说明 + 申购建议；中签率预测具名档位（甲头/甲中/甲大/甲尾/大甲尾；乙头/乙中/乙尾/顶头锤）+ 5 个真实校准案例（蜜雪/茶百道/古茗/巨星传奇/布鲁可）；招股信息完整字段（核心数据速览）；联网获取（新浪 best-effort，失败降级）
