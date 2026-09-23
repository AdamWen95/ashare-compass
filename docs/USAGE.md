# 使用指南

本项目生成只读的 A 股盘后研究日报。报告保留来源、观察时间、数据缺口与反面证据；不连接券商，不生成订单、仓位或买卖数量。先用合成数据运行离线演示，再根据自己的用途核对数据源权限。

## 环境与安装

在仓库根目录执行命令。项目要求 **Python 3.12**；锁文件分别针对 Windows x64 和 Linux x86_64。安装依赖需要访问包源，安装后的 DEMO 与离线检查无需网络，也不需要 `.env`。

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip --isolated install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip --isolated install --no-index --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\Activate.ps1
```

若 PowerShell 不允许执行激活脚本，可省略最后一行，将下文每个 `python` 替换为 `.\.venv\Scripts\python.exe`。不要覆盖已有的其他版本虚拟环境。

Linux x86_64：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip --isolated install --require-hashes -r requirements-linux.lock
.venv/bin/python -m pip --isolated install --no-index --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
source .venv/bin/activate
```

后续命令均使用上述虚拟环境中的 `python`。其他系统与架构的依赖组合需要自行验证。

真实新浪历史数据的解码还需要本机 Node.js。程序优先使用 Linux 项目内经哈希校验的 `.tools/node/bin/node`，否则查找 `PATH` 中的 `node`；DEMO 不需要 Node.js。可用 `node --version` 检查本机运行时。Linux 项目内安装说明见[部署说明](25_DEBIAN_DEPLOYMENT.md)。

## 离线演示

```bash
python -m ashare_daily doctor --offline
python -m ashare_daily brief --mode demo --date 2026-01-05
python -m ashare_daily brief --mode demo --date 2026-01-05 --empty-candidates
```

日期只是合成场景标签，不证明该日为交易日。证券、消息与指标均来自随代码分发的人工 fixture，不是历史行情或候选股推荐。`--empty-candidates` 展示零候选时的报告。

每次运行会输出 HTML、Markdown 和 JSON 的实际路径，默认写入：

```text
outputs/demo/<场景日期>/<运行编号>/
```

直接用浏览器打开生成的 `daily_brief.html`。也可追加 `--output-dir <目录>` 指定演示输出根目录。演示产物与真实研究产物分别存放；真实数据失败时不会用 DEMO 补齐。

## 当前观察流程

当前日观察入口须显式指定配置：

```bash
python -m ashare_daily run-daily --config config/sector_observation_daily.json --dry-run
```

该命令不访问外部数据源、不调用模型，会在 `outputs/research/m4/previews/` 写入预览记录。预览只检查本地配置与计划，不证明行情覆盖或接口连通。

**无参数 `run-daily` 仍使用 `config/sse_szse_daily.json` 的全市场行情流程。** 若要运行本指南的行业筛选和观察日报，请保留显式的 `--config`。

日观察按以下顺序处理：可信交易日历与动态沪深 A 股名单、行业筛选、冻结关注集合、仅为该集合准备必要历史、量价及资格核验、可选财务核查与新闻背景、冻结报告。当前范围暂不含北交所；没有符合规则的行业或个股时，零候选也是有效结果。

当前资格规则保留非 ST、非停牌、上市状态、证券身份及量价检查，不以退市整理状态作为正式候选资格条件，也不为该条件专门采集名单。未知资格仍显示待核查。参考技术评分只作辅助展示，不改变候选排序；公司公告与基本面缺口不会因此被视为已完成。

### 显式运行真实研究

仓库中的部分来源登记已经启用，执行真实命令前请阅读[数据源说明](DATA_SOURCES.md)，逐项核对自己的访问、存储、模型使用与再分发权限。历史登记中的授权或连通记录不代表当前使用者已获许可。

确认配置后，先运行不调用模型的真实流程：

```bash
python -m ashare_daily run-daily --config config/sector_observation_daily.json --skip-model
```

`--skip-model` 只跳过模型背景分析，仍可请求交易日历、名单、行情、资格证据及已启用的财务数据。完整离线预览必须使用 `--dry-run`。本项目没有统一的 `--online` 总开关：不同命令的网络边界如下。

| 命令或参数 | 网络行为 |
| --- | --- |
| `doctor --offline`、`brief --mode demo` | 不联网，不调用模型 |
| `run-daily --dry-run` | 只生成本地预览 |
| `run-daily --skip-model` | 可采集真实数据，跳过模型 |
| `doctor --source baostock`、`collect` | 实际请求 BaoStock |
| `market provider-check` | 默认离线；`--online` 且来源权限允许时请求样本 |
| `sector gaps`、`sector qualify` | 默认离线；补证须显式 `--online` |
| `sector select`、`sector prepare`、`sector resume` | 可采集真实资料；`--dry-run` 只预览 |
| `doctor --model` | 实际调用模型，可能产生费用 |

日观察使用北京时间。未指定 `--date` 时以运行当天作为待核验目标，21:00 前运行当日任务返回 `not_due`；交易日必须经可信日历确认，不能按星期推断。历史研究可追加 `--date YYYY-MM-DD`，将占位符替换为实际日期；缺少该日可验证输入时会保留缺口，不会把当前名单或后来采集的资料冒充当时实时结果。

`--market-max-seconds 1200` 可限制本次观察流程的时间预算；`--force` 会请求生成新版本，仍受预算约束。通常应先检查现有结果：相同冻结输入的成功报告可以复用，不需要强制重跑。运行命令不会自行安装定时任务。

### 可选模型背景

只有需要新闻背景解释时才配置模型。若 `.env` 不存在，可复制 `.env.example` 后在本机编辑；已有文件不要覆盖，密钥不要提交到仓库。当前适配器使用 HTTPS Chat Completions 协议，主要字段如下：

| 字段 | 用途 |
| --- | --- |
| `MODEL_PROVIDER` | 本地记录使用的服务标识 |
| `MODEL_BASE_URL` | 服务提供的 HTTPS 基址 |
| `MODEL_API_KEY` | 本机保存的密钥 |
| `MODEL_NAME` | 服务提供的模型名称 |
| `MODEL_PROTOCOL` | `chat_completions` |
| `MODEL_TIMEOUT_SECONDS`、`MODEL_MAX_RETRIES` | 超时和有限重试 |
| `MODEL_MAX_OUTPUT_TOKENS`、`MODEL_OUTPUT_TOKEN_PARAMETER` | 输出上限及服务支持的参数名 |
| `MODEL_MAX_CALLS`、`MODEL_MAX_INPUT_CHARS` | 请求数和输入字符预算 |
| `MODEL_RESPONSE_MODE` | `auto`、`text`、`json_object` 或 `json_schema` |

日观察从日任务配置的 `env_file` 读取这些字段，默认路径为项目根目录 `.env`；该流程不读取终端同名环境变量。日任务配置还会覆盖部分超时和预算值，当前每次最多 2 次模型请求、每日最多 6 次，重试也计入请求预算。

核对发送给模型的材料许可与预算后，去掉 `--skip-model`：

```bash
python -m ashare_daily run-daily --config config/sector_observation_daily.json
```

是否启用新闻背景还由 `config/sector_observation.json` 的 `model_context_enabled` 控制；缺少密钥或可用证据时会记录相应状态。行情指标由确定性代码计算，市场行情与财务数据不发送给模型。

来源开关在对应 JSON 配置中：例如 `config/sector_first.json` 的 `sina`、`config/sse_szse_universe.json` 的 `sources`、`config/m3_sources.json` 中各项 `registration`，以及 `config/sector_observation.json` 的 `financial_review`。仅修改 `.env` 中的 `ENABLE_WEB_READER` 或 `ENABLE_AKSHARE` 不能控制这些流程；当前实现没有读取这两个变量。

## 阅读报告与检查状态

启动本机只读查看器：

```bash
python -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

打开 [http://127.0.0.1:8501](http://127.0.0.1:8501)。网页读取已经生成的真实研究报告、历史版本和运行状态，并核验已登记文件的哈希；刷新页面不会触发采集或模型调用。没有真实存档的新仓库会显示暂无报告，DEMO 请直接打开生成的 HTML。

观察日报默认在 `outputs/research/sse_szse_a/observation_reports/`，包括 `daily_observation.html`、`daily_observation.md`、`daily_observation.json` 与 `screening_audit.csv`。实际版本路径以命令返回的 `report.directory` 为准。网页提供对应产物下载，工程验证报告有独立栏目。

检查命令返回的 `status`、`generation_status`、`module_statuses` 与 `exit_code`，不要仅凭文件存在判断成功。日观察退出码含义：

| 退出码 | 含义 |
| --- | --- |
| `0` | 完成、复用，或无需执行的状态；须同时看 `status`，如 `dry_run`、`not_due`、`non_trading_day` |
| `1` | 已生成部分结果，仍有数据或模型背景缺口 |
| `2` | 前置条件阻塞、超时或运行失败 |
| `3` | 已有任务持有共享锁，本次未执行 |

历史报告显示自己的行情日期、截点与实际生成时间。旧报告仍可阅读，不表示今天已经成功生成新报告。

## 本地验证

```bash
python -X utf8 -m pytest -q --basetemp .t/pytest
python -m ashare_daily --help
python -m ashare_daily run-daily --help
```

Windows 建议保留 `-X utf8` 和较短的 `--basetemp` 路径，避免默认 GBK 编码及深层路径限制。pytest 会清理该测试目录，不要在 `.t/pytest` 保存个人文件。

测试使用本地输入与替身，公共 fixture 阻止 Python socket 外连。部分平台或依赖本地档案的测试可能跳过，应查看本次实际摘要；测试通过不代表第三方来源已连通、许可已确认或真实全链路已完成。
