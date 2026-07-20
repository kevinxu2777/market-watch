# Market Watch Tool

本地运行的美股市场监控工具：盯全球财经新闻、宏观/地缘事件、大盘与商品指标（美股期货、VIX、美债收益率、原油、黄金、美元指数）和自选个股，给每条事件打分，重要的发邮件告警，全部数据落在本地 SQLite 和一个实时网页仪表盘里。

纯 Python 标准库实现，**没有任何第三方依赖**，不需要 pip install。

## 功能一览

- **新闻扫描**：多个 RSS 源按关键词加权打分，低分和重复报道自动过滤
- **大盘与商品**：标普/纳指期货、VIX、10 年期美债、原油、黄金、美元指数，超阈值告警
- **个股 watchlist**：价格异动、财报日期倒计时、多空催化剂新闻（政府合同/评级调整/诉讼等）
- **宏观日历**：FOMC 议息日程 + CPI/PPI/PCE/非农/GDP 等数据发布日提前提醒
- **邮件分级**：重大事件立即发，一般告警按小时合并发，低分事件只进每日摘要
- **实时仪表盘**：`http://127.0.0.1:8765`，带搜索/筛选/图表/健康指示灯/一键降噪反馈

## 快速开始

```bash
git clone https://github.com/kevinxu2777/market-watch.git
cd market-watch

# 跑一次（不需要任何配置）
python3 market_watch.py --once

# 持续监控（默认每 300 秒轮询，同时启动仪表盘）
python3 market_watch.py
```

运行后生成：

- `market_watch.sqlite3`：告警历史、去重状态、指标历史
- `dashboard.html`：跳转页，实际仪表盘在 `http://127.0.0.1:8765`

仪表盘顶部有**健康指示灯**：绿色表示监控在正常轮询；超过 2 倍轮询间隔没有成功轮询会变红，提示监控可能停了。仪表盘还支持搜索、类别/分数筛选、`Not important` 降噪反馈按钮和 `Send Test Email` 按钮。

跑测试：

```bash
python3 -m unittest test_market_watch
```

## 邮件告警配置

不配邮件也能用（告警只进仪表盘）。要开邮件，在项目目录创建 `.env.local`（已 gitignore，不会被提交）：

```bash
SMTP_USERNAME="your_email@gmail.com"
# 可选项（默认值如下）：
# SMTP_HOST="smtp.gmail.com"
# SMTP_FROM="$SMTP_USERNAME"
# ALERT_EMAIL_TO="$SMTP_USERNAME"     # 多个收件人用英文逗号隔开
```

Gmail 用户需要创建 [App Password](https://myaccount.google.com/apppasswords)（不是邮箱登录密码），然后运行一次：

```bash
./setup_gmail_password.command   # 密码存进 macOS Keychain，只需做一次
```

验证邮件通了：

```bash
./send_test_email.command
```

也可以不用 `.env.local` 和 Keychain，直接 export `SMTP_USERNAME` / `SMTP_PASSWORD` 等同名环境变量后运行 `python3 market_watch.py`。

## 邮件发送策略

告警邮件分三档，不会每次轮询都发：

1. **重大事件**（分数 ≥ `alerting.critical_email_score`，默认 85）：立即发，主题带 🚨，不受冷却限制
2. **一般告警**（分数 ≥ `alerting.min_email_score`，默认 55）：进待发队列，最多每 `alerting.batch_email_minutes`（默认 60）分钟合并发一封；期间出现重大事件会搭车一起发出
3. **低分事件**：只进仪表盘和每日摘要

每日摘要默认三次（`digest.times_local`）：`08:30` 盘前、`12:00` 盘中、`16:15` 收盘后，内容是市场快照 + 最近 12 小时高分事件。摘要发出后会清空待发队列，同一条告警不会在摘要和批量邮件里重复出现。想关摘要把 `digest.enabled` 改成 `false`。

## 自定义配置

```bash
cp config.example.json config.local.json
# 改 config.local.json 即可，.command 启动脚本会自动优先使用它（已 gitignore）
```

直接用 CLI 时手动指定：`python3 market_watch.py --config config.local.json`

常用可调项：

| 配置项 | 说明 |
|---|---|
| `poll_interval_seconds` | 轮询间隔，默认 300 秒 |
| `news.feeds` / `keywords` / `keyword_weights` | RSS 源、触发关键词和权重 |
| `news.source_weights` | 给更重要的新闻源加权 |
| `news.suppress_keywords` | 屏蔽词（个人理财、旅游、购物等） |
| `news.min_score` | 新闻入库最低分，默认 35 |
| `news.topic_cooldown_hours` | 同话题冷却时长，默认 4 小时 |
| `market_data.instruments` | 大盘/商品符号和涨跌幅阈值 |
| `equity_watchlist.stocks` | 自选股（名称、别名、主题、独立阈值） |
| `econ_calendar.events` | 要盯的宏观数据（CPI、非农等） |
| `fed.series` | FRED 序列和告警上下限 |
| `alerting.*` | 邮件分数阈值和批量间隔 |

## 监控内容详解

### 个股 watchlist

默认盯 MAG7（AAPL/MSFT/GOOGL/AMZN/META/NVDA/TSLA）和 AI 主题股（AVGO/AMD/TSM/MU/SMCI/PLTR/ARM/ORCL/ADBE），每次轮询更新价格、日涨跌幅和下次财报日期。

- **价格异动**：超过 `daily_move_alert_pct`（默认 3%，可按股票单独设）进告警；普通异动只上仪表盘，分数达到 `stock_price_move_email_score`（默认 85）的大异动才发邮件
- **催化剂新闻**：命中多空关键词的个股新闻优先发邮件，带 Bullish/Bearish 标签。Bullish 如政府合同、供应协议、大订单、上调指引、分析师上调；Bearish 如调查、诉讼、反垄断、下调评级、出口限制、SEC charges、网络攻击
- **财报提醒**：进入 `earnings_email_days_before`（默认 7 天）窗口时发邮件提醒。日期来自 Nasdaq earnings calendar（每天缓存一次）；配置 `FMP_API_KEY`（[Financial Modeling Prep](https://financialmodelingprep.com)）可提高远期覆盖率；显示 `not confirmed` 表示公开日历还没有确认日期，不是程序出错；也可在 `equity_watchlist.earnings_overrides` 手动指定：

```json
"earnings_overrides": {
  "AAPL": "2026-07-30 after hours, manual"
}
```

### FOMC 与宏观数据日历

- FOMC 日程来自美联储官网，默认会前 7/2/1/0 天提醒，仪表盘显示下次议息倒计时
- CPI、Core CPI、PPI、PCE、非农、失业率、GDP、零售销售的发布日程来自 Nasdaq economic calendar，默认发布前 1 天（medium）和当天（high，立即进邮件队列）提醒，仪表盘 Macro tab 显示 `Next CPI` 等倒计时卡片。默认向前看 14 天，结果按天缓存

### 美联储利率（可选）

配置 [FRED API key](https://fred.stlouisfed.org/docs/api/api_key.html) 后监控 EFFR（联邦基金有效利率）和 SOFR，超出配置区间告警：

```bash
export FRED_API_KEY="your_fred_key"
```

## 降噪机制

- **打分过滤**：关键词权重 + 新闻源权重 + breaking/市场相关加成，低于 `news.min_score` 不入库
- **话题冷却**：同一关键词组合（"换个说法的同一件事"）在 `topic_cooldown_hours`（默认 4 小时）内只告警一次；分数比之前高出 `topic_escalation_score`（默认 15）以上视为事态升级，仍会放行
- **异动去重**：同一标的同方向的价格异动 12 小时内只告警一次（方向反转算新事件）
- **人工反馈**：仪表盘每条告警有 `Not important` 按钮，点过的相似新闻以后自动降权

## 图表

仪表盘 Charts tab 画所有大盘指标的历史曲线，数据来自每次轮询的本地记录（至少两个轮询点才出线）。想快速补历史数据：

```bash
python3 market_watch.py --import-history --history-days 180
```

## 开机自启（macOS）

创建 `~/Library/LaunchAgents/com.yourname.market-watch.plist`（把两处路径换成你的 clone 位置）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yourname.market-watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/path/to/market-watch/run_market_watch.command</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>/path/to/market-watch/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/market-watch/launchd.log</string>
</dict>
</plist>
```

```bash
# 加载（开机自启 + 崩溃自动拉起）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yourname.market-watch.plist

# 查看状态 / 停止
launchctl print gui/$(id -u)/com.yourname.market-watch | grep state
launchctl bootout gui/$(id -u)/com.yourname.market-watch
```

启动早期错误看 `launchd.log`，运行日志看 `market_watch.log`。改了代码或配置需要 bootout + bootstrap 重启一次才生效。

## 文件说明

| 文件 | 用途 |
|---|---|
| `market_watch.py` | 主程序（单文件，纯标准库） |
| `test_market_watch.py` | 单元测试 |
| `config.example.json` | 默认配置；复制为 `config.local.json` 自定义 |
| `run_market_watch.command` | 双击启动（读 `.env.local` + Keychain） |
| `setup_gmail_password.command` | 一次性把 Gmail App Password 存进 Keychain |
| `send_test_email.command` | 发一封测试邮件验证配置 |
| `weekly_leap_review.py` | 作者的个人周报脚本，依赖仓库外的 trading-agent 项目，可忽略 |

## 常用 Yahoo Finance 符号

原油 `CL=F` · 黄金 `GC=F` · 标普期货 `ES=F` · 纳指期货 `NQ=F` · VIX `^VIX` · 10 年期美债 `^TNX` · 美元指数 `DX-Y.NYB` · 比特币 `BTC-USD`

## 注意事项

数据来自公开接口（RSS、Yahoo Finance、Nasdaq、美联储官网、FRED），可用性取决于网络和对应服务条款。所有 HTTPS 请求默认做完整证书验证；如果本机 Python 报证书错误，优先修复证书或设置 `SSL_CERT_FILE` 指向可信 CA bundle，仅在本地临时排查时才用 `MARKET_WATCH_INSECURE_SSL=1` 跳过校验，不要长期开着。

这个工具适合做预警和信息聚合，不应该单独作为交易决策依据。
