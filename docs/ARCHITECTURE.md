# 架构与开发

## 当前观察流程

```text
可信交易日历 + 沪深证券名单
  → 行业分类、一日行情、完整成分核验
  → 冻结行业选择和成分并集
  → 关注集合历史行情及增量缓存
  → 量价指标、证券资格、数据缺口
  → 可选财务补充 / 本地辅助评分 / 获准文字资料的模型背景
  → 冻结日报与引用校验
  → 只读网页与导出
```

`operations/observation.py` 协调当前日观察流程，入口配置为 `config/sector_observation_daily.json`。`cli.py` 还保留早期演示、样本与全市场命令，调用时应显式选择配置和用途。

## 目录

| 路径 | 作用 |
| --- | --- |
| `src/ashare_daily/cli.py` | 命令行入口 |
| `src/ashare_daily/providers/` | 来源适配、请求约束、原始响应处理 |
| `src/ashare_daily/storage/`、`quality/` | 数据保存和质量检查 |
| `src/ashare_daily/sector_*.py` | 行业选择、历史准备、筛选、资格和观察流程 |
| `src/ashare_daily/factors/`、`screening/` | 指标与规则计算 |
| `src/ashare_daily/research/` | 资料整理、证据、模型配置与调用 |
| `src/ashare_daily/reports/` | 冻结报告、校验、读取和渲染 |
| `src/ashare_daily/operations/` | 日任务、锁、预算和备份恢复 |
| `streamlit_app.py`、`src/ashare_daily/viewer*.py` | 只读阅读页面 |
| `config/` | 版本化流程、策略及来源配置 |
| `tests/` | 离线单元与集成测试 |
| `scripts/` | 安装、诊断、打包及维护工具 |

`data/`、`outputs/`、`logs/`、`backups/`、`deploy/` 和 `.local/` 是本地内容，不随源码分发。部分历史诊断、升级脚本依赖旧基线和原始运行材料，不属于新安装的必要步骤，参见[升级工具说明](31_RESEARCH_COMPLETION.md)。

## 数据契约

- 证券 ID 保留交易所；成交量按股、成交额按元保存，比例和百分数按适配器契约转换。
- 空值、停牌、来源失败和确认无数据分别处理，不补造 K 线，不把未知资格视为通过。
- 连续指标使用一致的来源、复权口径和版本；旧报告绑定原快照。
- 报告分别记录行情日期、资料截点、首次观察及实际生成时间，业务时区为 `Asia/Shanghai`。
- 外部资料是数据，不能改变程序权限或执行指令。模型解释必须引用有效证据，失败时显示缺口。
- 行情覆盖、策略输入是否就绪、资格是否通过分别统计；工程验证与正式研究隔离。

## 本地验证

依赖由 Windows / Linux 各自的 lock 文件锁定，Python 版本范围为 3.12。安装后在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m ashare_daily doctor --offline
.\.venv\Scripts\python.exe -X utf8 -m pytest --basetemp .t/pytest
```

Linux 使用 `.venv/bin/python`。Windows 使用 UTF-8 和短测试临时路径，避免默认编码与深层路径限制；pytest 会清理 `--basetemp` 指定的目录，不要在那里保存个人文件。针对改动可选择相应测试文件；跳过项及缺少本地档案应如实说明。离线测试成功不代表来源当前可达，也不证明市场数据已完整采集。

## 修改约定

先阅读 [AGENTS.md](../AGENTS.md) 和[研究范围](00_SCOPE_CHANGE.md)。保持 Demo / research 隔离、旧报告不可改写、模型预算持久化以及失败状态可追溯。不上传 `.env`、凭据、真实响应、数据库或私人部署材料。

提交说明应写明改动原因、验证命令和未验证范围。该项目的代理工作约定要求：只有用户明确要求时才创建 Git 提交、标签或推送。
