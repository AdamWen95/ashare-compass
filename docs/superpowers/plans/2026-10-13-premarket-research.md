# 每日盘前研究 Implementation Plan

> 2026-10-14 后续用户修正已实施为[证据分量v2决策](../../PREMARKET_EVIDENCE_ADR.md)：资料不足不再自动hold，按独立有效分量分析；新报告v2兼容读取旧v1。下文原计划中的相反预期只适用于历史v1，不作为当前验收规则。

> **For agentic workers:** Use superpowers:executing-plans to implement task-by-task in this session. Use test-driven-development; retain uncommitted work. The user explicitly requested implementation after specification review.

**Goal:** 实现每天 09:00 面向 3–10 交易日波段的盘前姿态、方向和股票研究，保留证据和失败状态。

**Architecture:** 新增纯计算的盘前领域模块，复用现有行情、来源、资格、评分和预算边界。独立早报 schema 与读者保留旧报告；单一日任务管理准备、消息截止和发布。

**Tech Stack:** Python 3.12、现有 Pydantic/Decimal/SQLite、Streamlit、pytest；不升级依赖。

**Spec:** `docs/superpowers/specs/2026-10-13-premarket-research-design.md`

## Global Constraints

- Asia/Shanghai；08:30 启动、08:50 行情截止、08:55 消息截点、08:59 决策截止、09:00 发布；09:15 起不自动补发。
- 3–10 个交易日；最多 3 个量价方向、2 个事件方向、5 只预选，每方向最多 2 只；不凑数。
- 模型每任务最多 2 次、每日最多 6 次、自动重试 0；新闻 HTTP 每轮总上限 20；每日定时尝试上限 2。
- 不导出行情、分数或财务原始数据；不新增账户、下单、仓位比例、数量或收益承诺。
- 新增手写文件不超过 500 行。旧冻结记录及其 shadow_only 语义不改写。
- 不运行 git add/commit/amend/tag/push；保护现有修改。按用户本轮实施指令在当前工作区执行，以逐文件基线与差异检查保护已有工作。
- 外部来源及真实盘前语义验收不足时，完成可独立验证的代码，但保留生产调度，明确剩余发布前置条件。

## Review Focus

- 休市日跨年后下一交易日未知，必须显示待核验而非猜星期；Task 1 覆盖。
- 只得到入选行业行情时，不能输出全市场姿态；Task 2 覆盖。
- 个股同时归属多个方向及原排名 20 名外高分股票，不能重复或漏排；Task 3 覆盖。
- 模型事件引用真实但主体、否定或生效时间错误，不能自动变成利好或风险；Task 4 覆盖。
- 崩溃发生在模型请求后、发布前，重启不能重置预算或覆盖已冻结结果；Task 6 覆盖。

## Task 1: 日期契约和截止

Files: `premarket/__init__.py`, `premarket/contracts.py`, `premarket/timing.py`；tests `test_premarket_timing.py`（源码根均为 `src/ashare_daily/`）。

Interfaces: `resolve_schedule(now: datetime, calendar: dict[str,bool], *, report_date: date|None=None) -> dict`；`request_window(schedule: dict, previous_cutoff: str|None) -> dict`；`stage_seconds(schedule: dict, stage: str, now: datetime) -> float`。

- [ ] 写日期边界测试：2026-10-12 早晨目标 10-12、行情日 10-09；10-11 休市版目标 10-12；缺未来日历拒绝；未来时间、无时区、非法序列拒绝。
- [ ] 运行 `python -m pytest tests/test_premarket_timing.py -q`，确认缺实现导致失败。
- [ ] 实现独立日期字段、明确 unknown 错误、精确阶段剩余秒数；恢复运行只使用 09:15 前剩余时间。
- [ ] 同一命令应全部通过；日志写入 `.local/implementation/premarket/`，不提交。

## Task 2: 市场环境指标和姿态

Files: `premarket/market.py`, `premarket/decisions.py`；tests `test_premarket_market.py`。

Interfaces: `market_snapshot(*, universe: dict, quotes: list[dict], benchmark: dict, market_date: str) -> dict`；`decide_stance(market: dict, *, news_ready: bool, has_direction: bool, adverse_event: bool=False) -> dict`。

- [ ] 测试全市场去重与分母、证实全天停牌、未日期化报价、21 日窗口、昨日/本日错配、NaN/布尔数值、成交额零值与缺失。
- [ ] 测试 40%/30% 防守边界、60% 进攻边界、MA20 相等、新闻未知和重大反面事件；预期原因码区分 data_insufficient、mixed、event_pending。
- [ ] 先运行该测试文件看失败，再实现 Decimal 计算、完整性证明和固定决策优先级。
- [ ] 运行该文件与 Task 1 文件，全部通过后记录。

## Task 3: 方向与股票优先级

Files: `premarket/candidates.py`；tests `test_premarket_candidates.py`。

Interfaces: `preselect_candidates(selection: dict, evaluations: list[dict], reference: dict) -> dict`；`finalize_candidates(preselection: dict, *, company_reviews: dict, stance: dict, events: list[dict]) -> dict`。

- [ ] 测试全部合格池重排、原排名 20 外高分、缺分不填值、同分稳定、每方向两只轮取、跨方向重复、少于五只不补凑。
- [ ] 测试公告未知降级、风险否决、不因否决追加外部核查、历史 shadow 数据不变。
- [ ] 先看到失败，再实现前 5 财务核查输入与最终重点/待查划分；可审计地记录每个候选的理由和排除原因。
- [ ] 对 Task 1–3 运行相关文件，全部通过后记录。

## Task 4: 来源覆盖、事件和公司核查

Files: `premarket/events.py`, `premarket/source_policy.py`, `premarket/model.py`，必要时扩展 `research/sources.py` 的已登记适配器；tests `test_premarket_events.py`, `test_premarket_sources.py`。

Interfaces: `validate_events(claims: list[dict], evidence: list[dict], *, cutoff: str, entities: dict) -> dict`；`coverage_summary(registry: dict, collection: dict, *, required_roles: list[str]) -> dict`；`run_event_context(...)->dict` 复用 BudgetLedger 与模型适配器。

- [ ] 测试伪引用、第一观察晚于截点、同名/集团/否定/撤回、未授权文本、公告目录截断、纯标题不能算正文核查、缺角色覆盖不能 ready。
- [ ] 先失败，再实现引用和实体校验、政策/监管/公司 coverage、仅文字模型输入及共享预算；正向判断不自动加技术分。
- [ ] 新来源逐站阅读当前主站声明和访问规则；每个待接入来源最多一次目录和一次文章样本、无重试。权限不足停止；未经验证保持 disabled。
- [ ] 运行测试与现有 `test_m3_sources*.py`、模型引用相关测试；报告真实验证和离线测试分别计数。

## Task 5: 不可变报告、读者与研究跟踪

Files: `premarket/report.py`, `premarket/archive.py`, `premarket/study.py`；tests `test_premarket_report.py`, `test_premarket_study.py`。

Interfaces: `build_brief(*, schedule, market, stance, candidates, events, coverage, previous, generated_at) -> dict`；`publish_brief(project: Path, report: dict, *, now: datetime) -> dict`；`scan_briefs(output_root: Path, *, now: datetime) -> tuple[list,list]`；`followup_observations(brief: dict, bars: dict, calendar: dict, *, as_of: str) -> dict`。

- [ ] 测试哈希篡改、路径/junction、提前草稿不可见、发布日/行情日分离、原报告不修改、所有导出同口径、XSS 与 CSV 注入。
- [ ] 测试 3/5/10 个交易日未成熟/缺失不补值、基线和评分组同时保存、价格变化不命名为账户收益。
- [ ] 先失败，再实现新 schema、状态和原因、原子发布、证据绑定、只读扫描与有界事后计算。
- [ ] 验证新报告及现有观察报告读者/导出测试。

## Task 6: 生产入口与有界编排

Files: `operations/premarket.py`, `premarket/services.py`, `operations/daily.py`、配置；tests `test_premarket_daily.py`。

Interfaces: `run_premarket_daily(*, project, config, config_path, target, cutoff, start, now, dry_run, scheduled, planned, max_seconds=None, skip_model=False, force=False) -> dict`；生产 adapter 复用现有冻结输入与采集，测试注入只允许临时项目。

- [ ] 写 dry-run 零网络、同日复用、旧工作流尝试计数、共享锁、09:15 截止、阶段超时、模型未知结果不退款/重试、发布前崩溃恢复测试。
- [ ] 先运行失败，再实现新 workflow 的显式路由；旧公开入口行为保持版本边界；必要提取原行情准备能力，禁止复制整条旧流程。
- [ ] 复用可信日历；现场快照必须源日期校验；当天证券资料与前一收盘日行情保持各自日期。
- [ ] 运行新任务测试、旧 observation daily、预算、锁、配置测试。生产正式配置只在来源与盘前验收满足后切换。

## Task 7: 网页和文档

Files: `viewer_premarket.py`, `viewer.py`，`AGENTS.md`, `README.md`, `docs/00_SCOPE_CHANGE.md`, `docs/USAGE.md`, `docs/REFERENCE_STRATEGY.md`；tests `test_premarket_viewer.py`。

- [ ] 写 Streamlit 测试覆盖姿态、方向、评分理由、数据不足、休市、迟到、历史报告和今日未生成；先失败再实现。
- [ ] 增加新报告入口与只读今日状态，不覆盖旧报告阅读；页面、Markdown、HTML 使用同一事实模型。
- [ ] 文档区分已实现能力、生产开关与未验收来源；旧范围冲突仅按本次用户授权更新。
- [ ] 临时数据真实浏览器核对；不发送消息、不用测试数据覆盖生产输出。

## Task 8: 总体验证、审查和发布条件

- [ ] 新增文件行数、差异、秘密及发布清单检查；核对既有修改未被覆盖。
- [ ] 运行完整离线 pytest；实际失败、平台跳过与新增回归分别说明。
- [ ] 根据 executing-plans 要求进行一次独立整体代码审查，修复重要问题并用回归测试验证。
- [ ] 有界验证新增来源和真实盘前报价语义；不重复付费请求。未满足时只交付本地实现及明确的上线障碍。
- [ ] 满足上线条件后按私有运维入口备份、发布，复用现有 timer 修改为 08:30；保留预算/历史/访问范围，保存回退材料。
- [ ] 自然 09:00 业务运行尚未发生时明确标为待验收，不宣称已完成。

## 执行记录

逐任务结果和必要裁决记录在 `.local/implementation/premarket/progress.md`；本计划不要求 Git 提交。用户连续两次明确要求执行，采用当前会话直接实施，计划作为执行清单，不重复询问同一功能授权。

2026-10-13 执行状态：时间、姿态、评分优先级、事件校验、财务合并、存档、编排及网页已有本地实现并完成离线验证。Task 4 的新增来源及逐公司公告接入尚未完成；Task 8 的真实盘前样本、生产切换和自然 09:00 验收尚未完成。不能将本计划整体标记完成或把候选配置当成已经上线。具体证据及限制保存在私有执行记录，公开说明见 `docs/PREMARKET_RESEARCH.md`。

2026-10-13 后续实施：Task 4 已补齐巨潮身份/目录/PDF核查、央行/证监会解析、共享HTTP预算、绝对截止及跨日报首次观察；代码和样本回放已验证。央行robots禁止读取、证监会访问规则未核验，因此两项自动采集关闭。宏观/监管来源替代与语义核验仍未满足，Task 8 的真实盘前日期、生产切换及自然09:00验收继续保留。前段“接入尚未完成”为本次继续执行前的状态，不能用解析完成替代来源可用。
