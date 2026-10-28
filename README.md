# AStockQuant · A 股短线选股系统

一个运行在 Windows / Linux / 云端，并可在 iPhone Safari 上访问的 A 股短线研究系统。  
第一阶段只做**真实行情分析、盘前/盘后选股、回测和风险管理**，不连接券商、不自动下单。

> 所有行情来自真实公开数据源。数据源失败时页面明确报错，**不会用模拟行情、随机数或伪造收盘价填充结果**。
> 本项目不承诺收益，所有评分、价格区间和回测结果仅用于研究。

---

## 1. 功能

| 模块 | 内容 |
|---|---|
| 市场总览 | 上证指数、深证成指、创业板指、沪深300；真实涨跌家数、成交额、涨跌停估算、行情日期与获取时间 |
| 盘前选股 | 只使用最近一个**已完成**交易日数据；严格裁剪到 `signal_date`，不引用目标日价格；生成候选、评分、理由、风险 |
| 盘后选股 | 收盘后使用完整日线；均线趋势、MACD、RSI、量能、突破/回踩、相对沪深300强度、波动率 |
| 个股分析 | 输入 `603986` 等 6 位代码；真实 K 线、成交量、MA5/10/20、MACD、RSI、支撑压力、波动率和风险提示 |
| 自选股 | 添加/删除、持久化、最新行情与最近一次盘前/盘后信号 |
| 策略回测 | 收盘信号→次日开盘成交；T+1、涨跌停、停牌、佣金、印花税、过户费、滑点；收益/回撤/胜率/交易次数/基准 |
| 风险管理 | 基于 ATR14、20 日支撑压力生成观察区间、止损参考、目标参考、风险收益比；仓位计算器 |
| 定时任务 | 独立 `scheduler.py`，APScheduler 常驻，或外部 Cron/GitHub Actions 调用 `--once` |
| 移动端 | 响应式布局；表格可横向滑动；关键数字在卡片/表格中直接可见，不依赖鼠标悬停 |

### 评分（100 分制）

| 维度 | 权重 |
|---|---:|
| 趋势强度 | 25 |
| 量价配合 | 20 |
| 动量表现 | 20 |
| 相对强度 | 20 |
| 风险控制 | 15 |

规则集中在 `scoring.py`，与数据获取、UI、回测解耦。盘前与盘后共用同一套 100 分权重，但通过不同的数据截止时点和候选过滤实现区分。

---

## 2. 项目结构

```text
AStockQuant/
├─ app.py                     # Streamlit 页面（仅 UI 编排）
├─ config.py                  # 路径、权重、成本、股票池参数
├─ data_provider.py           # 真实行情数据源、重试、缓存、校验、自检
├─ market_calendar.py         # Asia/Shanghai 交易日历与任务日期推导
├─ indicators.py              # MA/MACD/RSI/ATR/相对强度（纯函数）
├─ scoring.py                 # 100 分制独立评分
├─ stock_screener.py          # 盘前/盘后选股流水线
├─ stock_analysis.py          # 个股分析
├─ market_rules.py            # 涨跌幅限制等市场规则近似
├─ risk_manager.py            # 观察区间、止损、目标、仓位
├─ backtest.py                # 日线级事件驱动回测
├─ market_service.py          # 市场宽度聚合
├─ watchlist_service.py       # 自选股
├─ storage.py                 # 原子 JSON/CSV、缓存、任务锁
├─ scheduler.py               # 独立任务入口 / APScheduler
├─ charts.py                  # Plotly 图表
├─ requirements.txt
├─ pytest.ini
├─ tests/
│  ├─ test_indicators.py
│  ├─ test_scoring.py
│  ├─ test_risk.py
│  ├─ test_market_rules.py
│  ├─ test_screener.py        # 信号日裁剪 / 停牌排除
│  ├─ test_backtest.py
│  └─ test_live_data.py       # 真实行情集成测试
├─ scripts/register_windows_tasks.ps1
├─ .github/workflows/scheduled-jobs.yml
├─ Dockerfile
├─ docker-compose.yml
└─ data/
   ├─ results/                # 每日盘前/盘后 JSON（应持久化）
   ├─ cache/                  # 快照/日线/日历缓存
   ├─ state/watchlist.json
   └─ logs/
```

---

## 3. 数据源与时点纪律

### 3.1 数据源策略

| 数据 | 首选 | 备用 |
|---|---|---|
| 个股前复权日线 | AKShare（东方财富接口） | 东方财富直连 → Baostock（真实成交额） → 腾讯前复权 → 新浪（不复权，仅兜底并标记） |
| 指数日线 | 东方财富直连 | AKShare → Baostock → 新浪 |
| 指数实时/最新报价 | 新浪 `hq.sinajs.cn` | 腾讯 → AKShare |
| 全市场快照 | 新浪 `Market_Center` | AKShare / 东方财富 |
| 交易日历 | AKShare 新浪交易日历 | 本地缓存 → 工作日近似（明确标记不可靠） |

AKShare 的接口不是永久稳定契约，因此：

* `data_provider.py` 对每个接口都有超时、重试、字段校验和错误信息；
* 对连续失败的源做 5 分钟熔断，避免 160 只股票每只都等待同一个失效接口；
* 快照必须有可验证的行情日期；无法确认日期时拒绝用于选股；
* 日线必须通过 `high >= low`、`high >= close/open`、`low <= close/open` 等完整性检查；
* 备用源缺失成交额时，使用估算值会明确标记；选股器不会把“无真实成交额”当成真实流动性；
* 全市场快照失败且只有本地缓存时，界面显示“缓存，不是实时行情”；
* `python scheduler.py --job health --once` 或“风险与系统 → 数据源自检”会实际请求验证。

**本机验证记录（2026-10-28）**：AKShare 的 `stock_zh_a_hist` 与东方财富
`push2his` 被当前网络主动断开连接（`RemoteDisconnected`），而指数/快照/日历正常。
已做两层处理：

1. 修正 Windows 失效系统代理导致的 `ProxyError`（默认绕过注册表代理，见
   `ASTOCK_USE_SYSTEM_PROXY`）；
2. 服务器仍断连时，自动改用 **Baostock 真实前复权日线**，其成交额为真实值，
   不使用“价格×成交量”冒充成交额。

因此自检会如实显示 AKShare/东方财富 FAIL、Baostock OK，选股结果中写入
“使用备用真实数据源”的警告，不会静默降级。

### 3.2 盘前 / 盘后严格区分

盘前：

1. `signal_date` = 此刻最近一个已完成结算的交易日；
2. 股票池快照与所有日线裁剪到 `signal_date`；
3. 目标观察日 = 下一个交易日（09:30 前运行时为当天）；
4. 最后一根日线不等于 `signal_date`（停牌/数据滞后）的股票直接跳过；
5. 不读取目标日盘中价格、成交量、涨跌幅。

盘后：

1. 收盘结算后（默认 15:10 之后）取最近完整交易日；
2. 使用完整日线计算趋势/量价/MACD/RSI/突破/回踩；
3. 结果为下一交易日的重点观察池；
4. 与盘前结果分文件保存：`data/results/<signal_date>/premarket.json` 与 `postmarket.json`。

### 3.3 缓存

* 指数报价内存缓存约 30 秒；
* 全市场快照约 120 秒；
* 前复权日线 6 小时；收盘后的历史区间可在下一盘前复用（最多 36 小时）；
* 交易日历 12 小时并持久化；
* 每次成功行情都会带 `source / fetched_at / quote_date` 元数据，结果 JSON 中同时记录。

---

## 4. Windows 本地运行

### 4.1 环境

* Windows 10/11 64 位
* Python 3.10–3.12（推荐 3.12 64 位）
* 能访问公网行情接口

```powershell
cd D:\股票交易系统

# 1. 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 安装依赖
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3. 数据源自检（必须真实访问公网）
python scheduler.py --job health --once

# 4. 启动网页
streamlit run app.py
```

浏览器打开 `http://localhost:8501`。

首次运行会拉取股票池与日线，可能需要数分钟；进度会输出到终端。页面中的数据日期、获取时间、数据源都会分开显示。

### 4.2 iPhone Safari 访问

1. 电脑和 iPhone 连接同一 Wi-Fi；
2. 用 `ipconfig` 查看电脑局域网 IP，例如 `192.168.1.20`；
3. 启动时允许 Streamlit 监听局域网：

```powershell
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

4. iPhone Safari 打开 `http://192.168.1.20:8501`；
5. 如无法访问，在 Windows 防火墙放行 8501 端口（仅限可信局域网）：

```powershell
New-NetFirewallRule -DisplayName "AStockQuant 8501" -Direction Inbound `
  -Protocol TCP -LocalPort 8501 -Action Allow
```

云端部署时使用平台提供的 HTTPS 地址即可。

### 4.3 Windows 定时任务

方式 A：常驻调度器

```powershell
.\.venv\Scripts\python.exe scheduler.py --serve
```

方式 B：注册 Windows 任务计划程序

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_windows_tasks.ps1
```

任务每天 08:15 / 15:30 触发，`scheduler.py` 内部会再次核验真实交易日历，周末和节假日自动跳过；Windows 任务失败时按设置自动重试。日志：`data\logs\scheduler.log`。
Windows 任务计划使用**本机时区**，请把系统时区设为“(UTC+08:00) 北京，重庆，香港特别行政区，乌鲁木齐”；
`scheduler.py --serve` 内部固定使用 Asia/Shanghai。

方式 C：只跑一次（供外部调度器调用）

```powershell
.\.venv\Scripts\python.exe scheduler.py --job premarket --once
.\.venv\Scripts\python.exe scheduler.py --job postmarket --once
```

> 不建议让 Streamlit 进程承担定时任务。Streamlit 重启、云平台休眠、多人访问都会影响其可靠性；本项目将调度与网页分离。

### 4.4 没有云服务器时的临时公网访问

如果没有云服务器，但希望 iPhone 通过公网 HTTPS 临时访问，可使用仓库中的
Cloudflare Quick Tunnel 启动脚本。它会把本机 8501 端口映射到一个随机
`https://*.trycloudflare.com` 地址：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_public.ps1
```

脚本会：

* 生成并保存访问密码到 `data\state\app_password.txt`（该目录不提交 Git）；
* 启动 Streamlit、独立调度器和 cloudflared；
* 把公网 URL 写入 `data\state\public_url.txt`，并在终端打印 URL 和密码。

**这不是云服务器方案**：电脑休眠、关机、断网或隧道进程停止后链接立即失效，
且 Quick Tunnel 的随机域名会变化，Cloudflare 不提供 uptime 保证。需要真正
24/7 访问时，请使用 6.1 的 VPS/Docker 方案。`tools/cloudflared.exe` 不提交
仓库，换机器时需重新下载。

---

## 5. 测试

```powershell
# 单元测试 + 真实行情集成测试
.\.venv\Scripts\python.exe -m pytest

# 只跑不使用公网的规则/回测测试
.\.venv\Scripts\python.exe -m pytest -m "not integration"

# 只跑真实行情测试
.\.venv\Scripts\python.exe -m pytest -m integration -v

# 语法检查
.\.venv\Scripts\python.exe -m compileall -q app.py config.py data_provider.py indicators.py stock_screener.py backtest.py risk_manager.py scheduler.py
```

集成测试失败时会直接报出接口/网络错误，不会用合成数据替代。当前环境
AKShare/东方财富上游断连，该接口测试标记为 `xfail`（上游恢复后自动转为
XPASS），其余真实行情与 Baostock 备用源测试必须通过。最近一次完整结果：
**22 passed, 1 xfailed**（其中 1 个为上游接口探测的预期失败）。

---

## 6. 云端部署

### 6.1 推荐：VPS / Docker（网页 + 调度器常驻，持久化卷）

适用于有公网 IP 的 VPS、NAS、云服务器。`docker-compose.yml` 同时启动：

* `web`：Streamlit；
* `scheduler`：APScheduler 常驻，08:15 / 15:30（Asia/Shanghai），并在 08:40 / 16:15 自动检查补跑；
* 两个容器共享命名卷 `astock_data`，挂载到 `/data`。

```bash
docker compose up -d --build
docker compose logs -f scheduler
```

访问 `http://服务器IP:8501`。手机 Safari 直接打开该地址；正式环境建议在前面加 Nginx/Caddy HTTPS 和访问认证。

不使用 Docker 时，`deploy/` 提供 systemd 模板（Web + Scheduler 两个独立进程）
和 Nginx 反向代理示例；两个服务必须共用 `ASTOCK_DATA_DIR`，并且该目录不要
放在临时文件系统。

更新代码：

```bash
git pull
docker compose up -d --build
```

容器内时区为 `Asia/Shanghai`，结果写入 `/data/results`。**只要 scheduler 容器不被平台休眠/停止，任务就会真正执行。**

### 6.2 Railway / Render / 云主机

1. 将仓库部署为 Web Service，启动命令 `streamlit run app.py --server.address 0.0.0.0 --server.port $PORT`；
2. 挂载持久化磁盘到 `/data`，设置 `ASTOCK_DATA_DIR=/data`；
3. 另建一个 Worker/Background Service，启动命令 `python scheduler.py --serve`，挂同一个磁盘；
4. 若平台不支持长期后台进程，改用 6.3 的外部调度。

### 6.3 Streamlit Community Cloud + GitHub Actions（备用方案）

Streamlit Community Cloud 适合展示，但不保证常驻后台任务，文件系统也可能在重启时重置。仓库已提供 `.github/workflows/scheduled-jobs.yml`：

* GitHub Actions 在 UTC `00:00`（北京 08:00）和 `07:30`（北京 15:30）运行；
* 执行 `scheduler.py --job ... --once`；
* 将 `data/results` 提交回仓库；
* Streamlit Cloud 检测到新 commit 后重新部署并读取最新结果。

启用步骤：

1. 将项目推送到 GitHub；
2. 仓库 Settings → Actions → General，Workflow permissions 选择 **Read and write**；
3. Actions 中手动运行一次 `AStockQuant scheduled jobs` 验证；
4. 在 Streamlit Community Cloud 部署同一仓库；
5. 检查 `data/results/<日期>/premarket.json` 是否随 commit 更新。

注意：GitHub 定时任务可能延迟数分钟，且连续 60 天无仓库活动会被暂停；若对 09:00 前完成有严格要求，应使用 6.1 的常驻调度器或云厂商 Cron 调用 `--once`。

### 6.4 外部 Cron 调用

任何能定期执行命令的外部调度器（云厂商 Cron、cron-job.org 的 Webhook + 自建 runner、
GitHub Actions 等）都可以调用：

```bash
cd /app && python scheduler.py --job premarket --once
```

必须保证：

* 使用 `Asia/Shanghai` 时区；
* 盘前留出足够时间（建议 07:50–08:15 启动，任务通常数分钟）；
* 结果目录是可持久化、Web 进程可读取的共享卷；
* 任务失败会返回非 0 退出码，便于外部系统告警/重试。

---

## 7. 配置项

通过环境变量覆盖常用参数（见 `config.py`）：

| 变量 | 默认 | 说明 |
|---|---:|---|
| `ASTOCK_DATA_DIR` | `./data` | 数据目录，云端建议 `/data` |
| `ASTOCK_TIMEOUT` | 20 | 单请求超时秒数 |
| `ASTOCK_RETRIES` | 2 | HTTP 重试次数 |
| `ASTOCK_WORKERS` | 6 | 日线并发数 |
| `ASTOCK_USE_SYSTEM_PROXY` | 0 | 1=使用 Windows 系统/显式代理；默认绕过失效的注册表代理 |
| `ASTOCK_APP_PASSWORD` | 空 | 非空时启用网页访问密码；公网部署必须设置 |
| `ASTOCK_UNIVERSE_SIZE` | 220 | 按成交额选取的股票池大小 |
| `ASTOCK_MAX_HISTORY_FETCH` | 160 | 单次实际拉取日线的股票数 |
| `ASTOCK_MAX_CANDIDATES` | 15 | 最终候选数上限 |
| `ASTOCK_MIN_SCORE` | 55 | 候选最低分；不足不补足 |
| `ASTOCK_MIN_AMOUNT` | 80000000 | 20 日平均成交额门槛（元） |

回测成本默认：佣金万三（最低 5 元）、卖出印花税 0.05%、过户费 0.001%、双边滑点 10bp。可在 `BacktestConfig` 中调整。

---

## 8. 明确限制（不能假装已实现）

1. 股票池先按信号日成交额取流动性靠前样本，**不是全市场 5000+ 只逐只扫描**；结果 JSON 的 `selection_stats` 会披露。
2. 日线为前复权；复权因子含除权信息，回测价格路径与当时实际报价可能存在差异。
3. 涨跌停按代码/名称近似（主板 10%、创业板/科创板 20%、ST 5%），**无法还原历史 ST 状态、新股上市初期无涨跌幅限制、退市整理期规则**。
4. 回测为日线级：收盘决策、次日开盘成交；未模拟集合竞价排队、盘口深度、冲击成本、融券和分红税。
5. 回测股票池来自当前仍存在的标的，存在幸存者偏差。
6. 选股未纳入公告、财报、行业轮动、龙虎榜、新闻、盘中盘口等场外信息。
7. 所有“观察区间/止损/目标/风险收益比”都是基于历史行情的规则计算结果，不是成交承诺或收益承诺。
8. 第一阶段不连接券商、不自动下单；任何交易决定需人工复核并自行承担风险。

---

## 9. 安全提示

程序支持通过 `ASTOCK_APP_PASSWORD` 启用应用内密码门禁；公网部署必须设置非空
密码，并建议在 Nginx / Cloudflare Access 再加一层访问控制。不要把未设密码的
8501 端口直接裸露到公网。系统不连接券商、不保存交易密码，也不执行任何下单操作。

## 10. 免责声明

本项目是量化研究与工程示例，不构成证券投资咨询或买卖建议。A 股市场存在政策、流动性、涨跌停、停牌、除权除息等风险，历史回测与评分不能保证未来表现。使用前请自行核验数据、遵守所在地法律法规和交易规则。
