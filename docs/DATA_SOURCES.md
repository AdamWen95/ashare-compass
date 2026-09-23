# 数据来源与访问边界

本页说明仓库的接入方式与配置边界，不作为第三方数据使用授权，也不证明来源此刻在线。软件适配器可用、请求成功、数据完整和许可适用应分别核验。

## 接入位置

| 来源或能力 | 仓库入口 | 使用说明 |
| --- | --- | --- |
| BaoStock | `providers/baostock*.py`、`financial_review.py` | 用于日历、基础资料、日线及可选财务补充；保留字段、日期和复权检查 |
| 沪深交易所公开名单 | `providers/exchange_universe.py`、`config/sse_szse_universe.json` | 校验板块、证券身份和观察日期；当前名单不能冒充历史名单 |
| 行业分类 | `providers/exchange_industry.py`、`config/sector_first.json` | 从名单字段形成版本化分类，不等同于所有供应商行业或概念体系 |
| 新浪网页数据 | `providers/sina_*.py`、`config/sector_first.json` | 网页来源，不称为对外授权的官方免费 API；部分历史解码需要 Node.js |
| 东方财富适配器 | `providers/eastmoney.py`、`config/sse_szse_market_providers.json` | 可选路径，不能把代码存在当成已完成在线验收或取得使用权 |
| 新闻与政策文字 | `research/sources.py`、`config/m3_sources.json` | 按来源登记访问、缓存、模型使用与摘录权限，覆盖范围独立显示 |
| 外部模型 | `research/model_settings.py`、本机 `.env` | 可选 Chat Completions 文本 JSON 路径；服务能力需单独验证 |

BaoStock 的实现约定见 [BaoStock 接入说明](09_BAOSTOCK_VERIFICATION.md)。Node.js 的安装工具为 `scripts/install_node_runtime.py`，其版本、下载地址和哈希保存在 `config/node_runtime.json`；它不是离线 Demo 的依赖。

## 配置不是通用授权

仓库保留了开发时的配置版本。部分配置的 `enabled`、`user_authorized` 或 `permission_status` 已启用，并带有历史核对时间及个人研究用途说明。这些是原运行上下文的记录，不能直接解释为来源对新使用者、商业用途或公开再分发的许可。

启用真实请求前，应检查选定配置及其引用的子配置，按实际用途核对来源规则、频率限制和数据日期。不要仅依赖 `.env.example` 的 `ENABLE_WEB_READER` 等字段判断整个程序是否会联网；具体命令及来源配置共同决定访问行为。可先使用 `--dry-run` 查看日流程。

权限不足、验证码、登录要求或访问限制应停止该源；限流遵循服务要求并有限退避。不轮换 IP、不多账号规避、不把 HTTP 200 的验证页面当作有效数据。

## 缓存、模型和发布

采集、缓存、向外部模型发送与公开发布需要分别核对许可。当前观察配置将市场数据模型导出关闭；财务资料和参考策略分数也限定本地使用。新闻与政策背景仅使用来源登记中允许的文字内容。

仓库仅分发程序和演示夹具，不附生产数据库、原始响应、新闻全文或个人生成的研究报告。复核资料时应保留来源与版本；完整性未知时写明缺口，不把“未获取”写成“没有风险”。

公开代码本身不授予数据源许可；项目依赖各自的许可证也不能替代数据使用许可。
