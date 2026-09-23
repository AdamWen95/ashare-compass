# 参考策略辅助评分

当前观察流程提供一组本地技术辅助分数，用于查看指标构成，不改变正式候选资格或原有排序。配置位于 `config/sector_observation.json`，固定为 `application=shadow_only`、`changes_candidate_ranking=false`、`llm_export=false`。

## 参考与实现

规则参考项目为 [Intelligent-stock-selector 的固定版本](https://github.com/ktoking/Intelligent-stock-selector/tree/c1bc1797d0c0f314b728f7d9e7639830e87ff0e4)，提交号 `c1bc1797d0c0f314b728f7d9e7639830e87ff0e4`。该出处同样保存在 `src/ashare_daily/reference_strategy.py`，用于结果追溯，不代表上游对本项目背书或授予许可。

本项目的指标计算、来源绑定和结果校验位于 `reference_indicators.py`、`reference_strategy.py` 与 `reference_study.py`。只适配七组技术规则，不复刻上游完整模型评分、估值、机构观点或期权分析。

| 因素 | 本地规则概要 |
| --- | --- |
| 趋势 | 收盘价与 MA5、MA10、MA20、MA60 的排列 |
| MACD | 12/26/9，区分金叉和 DIF 状态 |
| 随机指标 | 14 日窗口和 3 日均值，不等同于常见 9 日 KDJ |
| RSI | 14 日及固定阈值 |
| 背离 | 用已观察日线确认局部极值 |
| 成交量 | 当日量与含当日的 20 日均量比较 |
| 动量 | 20 日价格变化与固定阈值比较 |

使用固定 120 个交易日窗口。输入缺失、来源不符或无法计算时保留缺口，不填默认分数代替有效结果。输出绑定证券集合、交易日、输入哈希及观察版本。

分数不是上涨概率，也没有证据证明它优于原有排序。离线对照脚本 `scripts/compare_reference_strategy.py` 只描述冻结样本的后续价格变化，不能解释为可成交回测、账户收益或稳定获利能力。
