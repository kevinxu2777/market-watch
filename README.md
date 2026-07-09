# Market Watch Tool

一个本地运行的市场事件监控工具，用来盯全球新闻、宏观/地缘事件、关键金融指标、原油、黄金、美股期货、VIX、美债收益率。发现新异常后会写入 SQLite、更新 HTML 仪表盘，并在 SMTP 配置完整时发送邮件。

## 开机自启（推荐方式）

监控已注册为 macOS LaunchAgent（`~/Library/LaunchAgents/com.kevinxu.market-watch.plist`）：开机自动启动、进程挂掉后 launchd 自动拉起，不需要手动开终端。

常用命令：

```bash
# 查看状态
launchctl print gui/$(id -u)/com.kevinxu.market-watch | grep state

# 手动停止（下次开机仍会自启）
launchctl bootout gui/$(id -u)/com.kevinxu.market-watch

# 重新启动 / 改完代码后重启生效
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kevinxu.market-watch.plist
```

启动早期错误写在 `launchd.log`，运行日志在 `market_watch.log`。改了代码或配置后需要 bootout + bootstrap 重启一次才生效。

## 快速开始

```bash
python3 market_watch.py --once
```

运行一次后会生成：

- `market_watch.sqlite3`：告警去重和历史记录
- `dashboard.html`：本地仪表盘，每 60 秒自动刷新

持续监控：

```bash
python3 market_watch.py
```

默认轮询间隔是 300 秒，可以在 `config.example.json` 里改 `poll_interval_seconds`。

持续运行时还会启动实时网页仪表盘：

```text
http://127.0.0.1:8765
```

实时仪表盘支持搜索、类别筛选、分数筛选，以及 `Not important` 反馈按钮。点过的类似新闻以后会被降权。

页面顶部有监控健康指示灯：绿色表示后台监控在正常轮询；如果超过 2 倍轮询间隔没有新的成功轮询，会变红提示"monitor may be stopped"，避免只看页面时间误以为监控还活着。

仪表盘右上角有 `Send Test Email` 按钮，可以随时发送一封当前格式的测试邮件。也可以在终端运行：

```bash
python3 market_watch.py --send-test-email
```

如果已经把 Gmail App Password 存进 Keychain，也可以直接双击：

```text
send_test_email.command
```

所有 HTTPS 请求默认做完整证书验证（本机已验证正常）。如果哪天 Python 报证书校验错误，优先安装/修复 Python 证书，或设置 `SSL_CERT_FILE` 指向可信 CA bundle；只在本地临时排查时才可以用 `MARKET_WATCH_INSECURE_SSL=1` 跳过校验，不要长期开着。

## 邮件告警配置

建议用环境变量保存密码和 token：

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_USERNAME="your_email@gmail.com"
export SMTP_PASSWORD="your_app_password"
export SMTP_FROM="your_email@gmail.com"
export ALERT_EMAIL_TO="target@example.com"
```

多个收件邮箱用英文逗号隔开：

```bash
export ALERT_EMAIL_TO="first@example.com,second@example.com,third@example.com"
```

如果使用 Gmail，需要创建 App Password，而不是直接使用邮箱登录密码。

## 邮件频率策略

告警邮件分三档，不再每次轮询都发：

1. **重大事件**（分数 >= `alerting.critical_email_score`，默认 85）：立即发邮件，不受冷却限制，主题带 🚨
2. **一般告警**（分数 >= `alerting.min_email_score`，默认 55）：进入待发队列，**最多每 `alerting.batch_email_minutes`（默认 60）分钟合并发一封**；如果期间出现重大事件，会搭车一起发出
3. **低分事件**：只保存到仪表盘，出现在每日摘要里

摘要邮件发出后会清空待发队列并重置冷却计时，避免同一条告警重复出现在摘要和批量邮件里。

## 降噪和打分

工具会给每条新闻和市场事件打分：

- `news.min_score`：低于这个分数的新闻不会进入告警
- `alerting.min_email_score`：低于这个分数的告警只保存到仪表盘，不发邮件
- `news.keyword_weights`：越重要的关键词权重越高
- `news.source_weights`：给更重要的新闻源加权
- `news.suppress_keywords`：过滤个人理财、旅游、购物等低相关内容
- `news.topic_cooldown_hours`（默认 4）：同一关键词组合的"换个说法的同一件事"在冷却期内只告警一次；分数比之前高出 `topic_escalation_score`（默认 15）以上视为事态升级，仍会告警

默认摘要时间在 `digest.times_local`：

- `08:30`：盘前
- `12:00`：盘中
- `16:15`：收盘后

摘要邮件会包含当前市场快照和最近 12 小时高分事件。想关闭摘要，把 `digest.enabled` 改成 `false`。

## 个股、AI 和 MAG7

`equity_watchlist` 会监控 MAG7 和 AI 相关个股，默认包括：

- MAG7：`AAPL`、`MSFT`、`GOOGL`、`AMZN`、`META`、`NVDA`、`TSLA`
- AI：`AVGO`、`AMD`、`TSM`、`MU`、`SMCI`、`PLTR`、`ARM`、`ORCL`、`ADBE`

每次轮询会更新价格、日涨跌幅和下一次财报日期。个股达到设定涨跌幅阈值时，会进入告警。

邮件里的个股提醒会优先发送“催化剂新闻”，而不是普通价格波动。例如：

- Bullish：政府合同、政府投资、合作、供应协议、大订单、上调指引、分析师上调、AI/data center/cloud deal
- Bearish：调查、诉讼、反垄断、下调评级、下调指引、出口限制、禁令、召回、SEC charges、网络攻击

普通个股价格异动仍会保存到网页和数据库，但默认只有分数达到 `stock_price_move_email_score`，也就是很大的异动，才会发邮件。

财报日期来自 Nasdaq earnings calendar，默认向前查 120 天，并在当天缓存结果，避免每 5 分钟重复查询。

如果想提高 MAG7 等远期财报日期覆盖率，可以配置 Financial Modeling Prep：

```bash
export FMP_API_KEY="your_fmp_key"
```

程序会优先使用 FMP earnings calendar，其次使用 Nasdaq。没有确认日期时会显示 `not confirmed`，这通常表示公开日历源还没有返回确认日期，而不是程序崩了。

邮件不会每天重复发送所有财报日期。只有当某只 watchlist 股票进入 `earnings_email_days_before` 窗口，默认 7 天内，才会作为邮件提醒。

也可以在 `equity_watchlist.earnings_overrides` 手动填你确认过的日期，例如：

```json
"earnings_overrides": {
  "AAPL": "2026-07-30 after hours, manual"
}
```

## FOMC 日程

`fomc_calendar` 会从美联储官网读取 FOMC 日程，并在仪表盘里显示下一次议息决定日期。默认会在会议前 7 天、2 天、1 天和当天生成提醒。

## 宏观数据日历

`econ_calendar` 从 Nasdaq economic calendar 读取美国宏观数据发布日程（默认盯 CPI、Core CPI、PPI、PCE、Nonfarm Payrolls、失业率、GDP、零售销售），在仪表盘 Macro tab 显示 `Next CPI` 等卡片，并在发布前 1 天和当天生成告警（当天为 high，会立即进邮件队列）。默认向前看 14 天，结果按天缓存。

## 图表

实时网页仪表盘有 `Charts` tab，会画出本地记录的关键指标历史曲线，包括 VIX、10 年期美债收益率、原油、黄金和美元指数。图表数据来自每次轮询保存的本地历史，至少需要两个轮询点才会显示曲线。

## 可选数据源

### 美联储利率

工具支持 FRED。申请 FRED API key 后：

```bash
export FRED_API_KEY="your_fred_key"
```

默认监控：

- `EFFR`：Effective Federal Funds Rate
- `SOFR`：Secured Overnight Financing Rate

## 自定义监控

复制配置文件后修改：

```bash
cp config.example.json config.local.json
python3 market_watch.py --config config.local.json
```

可以调整：

- `news.feeds`：RSS 新闻源
- `news.keywords`：新闻触发关键词
- `news.high_impact_keywords`：高优先级关键词
- `market_data.instruments`：Yahoo Finance 符号和日涨跌幅阈值
- `fed.series`：FRED 序列和上下限
- `email`：SMTP 邮件设置

## 常用 Yahoo Finance 符号

- 原油期货：`CL=F`
- 黄金期货：`GC=F`
- 标普 500 期货：`ES=F`
- 纳指 100 期货：`NQ=F`
- VIX：`^VIX`
- 10 年期美债收益率：`^TNX`
- 美元指数：`DX-Y.NYB`

## 注意事项

新闻源、Yahoo Finance 和 FRED 的可用性取决于网络和对应服务条款。这个工具适合做预警和信息聚合，不应该单独作为交易决策依据。
