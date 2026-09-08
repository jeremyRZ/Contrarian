# 阿里云运行配置

生产项目：`/home/admin/Contrarian`。独立研究数据：`/home/admin/contrarian-research/.runtime/strategy-research`。

`contrarian-research.timer` 在周一至周五北京时间17:10运行，服务器重启后补执行错过的任务。遇到休市日，不会把历史股票池伪装成当日事前记录。脚本直接使用生产仓库内已提交的版本，研究数据库保留在独立目录。无需本机Codex在线，不发送企业微信或真实订单。

安装或更新前备份已有单元文件，再执行：

```bash
sudo install -m 644 deploy/contrarian-research.service /etc/systemd/system/
sudo install -m 644 deploy/contrarian-research.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now contrarian-research.timer
systemctl list-timers contrarian-research.timer
```

手动验证一次完整采集和计算：

```bash
sudo systemctl start contrarian-research.service
systemctl show contrarian-research.service -p Result -p ExecMainStatus
journalctl -u contrarian-research.service -n 30 --no-pager
```

结果为独立目录下的`report.json`、`REPORT.md`和`research.sqlite3`。每次研究运行会先备份已有数据库。采集失败时服务失败并写入日志，不继续用混合数据生成新报告。

通知偏好写入未跟踪的`config.yaml`，例如本次用户要求：

```yaml
notifications:
  risk_alerts_enabled: false
  muted_codes: ["HK.08305"]
```

此设置拦截持仓风险、每日持仓资金面报告及静音股票的消息，并覆盖通知失败重试。策略计算和账户数据保持独立。不要提交真实配置、密钥、Webhook或运行数据库。

回滚应用应使用部署前记录的Git提交和配置备份；暂停独立研究调度使用`sudo systemctl disable --now contrarian-research.timer`，保留研究数据，不删除数据库。服务器调度验证成功后，停用本机重复的Codex定时任务。
