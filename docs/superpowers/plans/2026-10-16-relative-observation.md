# 市场姿态与相对强势观察分离

> 执行依据：用户2026-10-16明确确认按此思路修改、部署并补跑。沿用既有盘前研究设计；不创建Git历史。

**Goal:** 市场防守不再使观察名单停止分析，输出可追溯的相对强势方向和股票、观察/失效条件及风险。

**Architecture:** 市场姿态继续只消费全市场广度、指数及事件。独立的盘前选择器从完整冻结行业/报价构建有限观察池，复用历史、评分和资格核验；报告v3显示观察名单，旧v1/v2保持原语义读取。

**Tech Stack:** 当前Python及锁定依赖，不新增来源、依赖或定时任务。

## 规则与接口

- 新模块 `premarket/selection.py` 提供 `build_observation_selection(strict_selection, packet)`。输入是既有 `evaluate` 的完整校验结果与绑定的原始packet，不接受不完整比较、日期/身份/hash冲突。
- 市场基准为同日已核验普通A股（剔除有据全日停牌）的等权平均涨跌幅及上涨家数占比。行业须样本至少5只、平均涨幅高于该基准、上涨占比不低于该基准、成交额为正。按相对超额/上涨占比/稳定ID排序取最多3个；允许行业绝对下跌，明确“相对抗跌”。不是收益预测。
- 每个方向从有效同日股票中按相对该行业及全市场的日涨跌幅选最多5只研究对象；两项超额均须为正，当日成交额至少5000万元。排除已知ST/停牌/非普通A；未知资格不冒充通过。最多15只进入历史及资格核查，覆盖外不宣称已深入分析。
- 新选择仍是哈希绑定的selection数据契约，但 `rule_version=premarket-relative-observation-v1`、`observation_only=true`、`basis_selection_id` 指向原严格选择。重新计算content_hash/id；原输入不修改。
- `selected_sectors` 记录 `direction_kind=relative_strength`、`relative_metrics`（`mean_change_pct`、`median_change_pct`、`advancing_fraction`、`market_mean_change_pct`、`market_advancing_fraction`、`excess_market_pp`、`absolute_up`、`member_count`）。
- 观察池成员记录 `relative_observation`（`sector_id`、`sector_mean_change_pct`、`market_mean_change_pct`、`stock_change_pct`、`excess_market_pp`、`excess_sector_pp`、`amount_cny`、`market_as_of_date`、`kind=single_session_relative_strength`）。数值由确定性代码生成。
- `preselect_candidates` 对 `observation_only` 选择启用新分支：证券资格fail或技术not_applicable拒绝；资格待核、评分缺失、技术fail可以进入待核观察，不能称为完整确认。有效评分优先、20日相对表现/单日超额/流动性用于稳定排序；方向轮选后最多5只。
- 新预选带 `observation_only=true`，新版 `finalize_candidates` 返回 `watch_candidates`。已知公司不利事实否决；未知核查保留明确风险，市场defend/hold/undetermined不清空观察。focus/pending旧字段可保持为空，新v3界面只显示watch一次。
- 观察项必须有理由、`watch_conditions`、`invalidation_conditions`、`observation_risks`，分别说明单日强弱、已验证历史趋势、资格和公司资料状态，禁止把跌得少写成上涨趋势。
- `decide_stance(..., independent=True)` 使用 `premarket-stance-v3`；不根据观察方向数量改变市场姿态，也不把方向数嵌入市场结论。默认旧分支保留v2，用于冻结报告校验。
- `build_brief` 在 observation_only 分支生成 `premarket-brief-v3`、recommendation `premarket-observation-v1`，新字段watch_candidates；旧分支仍生成v2。READABLE_SCHEMAS增加v3，旧归档不改。
- 调用链：收集原packet → evaluate严格选择并归档 → build_observation_selection并归档 → 最多15只历史/资格核查 → 预选最多5只公司资料 → 独立市场姿态与观察名单 → v3报告、页面和导出。

## 验收与运行边界

- [x] 选择器RED→GREEN：全行业下跌仍能选相对强势方向；相同弱势、数据缺失/日期冲突不能硬凑；预算池最多15，选择内容hash可重放。
- [x] 观察名单RED→GREEN：防守/按兵不动保留合法观察；已知ST/停牌/资格fail/公司adverse排除；缺评分/趋势未确认明确标注；最多5且不重复。
- [x] 报告及市场姿态RED→GREEN：姿态不受观察数量影响；条件风险同时进入网页/HTML/Markdown/JSON；旧v1/v2仍可核验。
- [x] 集成与独立审查、全量离线回归、服务器隔离测试及今晨冻结数据演练。
- [x] 精确文件备份部署，保持原调度/配置/预算；一次有界补跑。只复用今晨名单/报价，新增历史和消息按实际时点记录，保护原报告。

市场阶段最多1200秒，消息20请求/300秒，模型每运行2次、每日6次且模型重试0；阶段失败保留已经核验的证据和未知状态。不以启动/返回ID代替完成。发布与补跑细节、内网位置及证据只记录在忽略的私有运维目录。
