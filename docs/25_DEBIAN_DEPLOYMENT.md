# Linux 部署与备份

本文面向使用者自己的 Linux 主机，不包含任何现有服务器地址、账号或生产状态。先按[使用指南](USAGE.md)建立 Python 3.12 虚拟环境并完成离线演示；Windows 的 `.venv` 不能直接搬到 Linux 使用。

## 本地部署参数

部署脚本不再内置某台服务器的地址、账号或目录。首次配置时，把 [deployment.example.json](../config/deployment.example.json) 复制到项目根目录的 `.local/deployment.json` 并填写实际值；已有文件不要覆盖。模板里的账号和地址都是示例。

| 字段 | 用途 |
| --- | --- |
| `project_root` | 升级安装目标的 Linux 绝对目录 |
| `account` | Linux 服务所属普通用户 |
| `ssh_host` | 本机 SSH 配置中的别名或主机名；SSH 用户、端口与认证由正常 SSH 配置管理 |
| `server_ip` | Nginx 入口绑定的内网 IPv4 地址 |
| `listen_port` | Nginx 入口端口 |

配置只接受这些部署字段及 `schema_version`，不保存密码、Token 或私钥。`.local/` 已被 Git 忽略，也不在源码与数据备份的默认打包目录中；迁移时需要在目标环境单独准备该配置，或显式传入参数。

### 维护入口与私有交接

后续开发与发布以 `ashare-compass` 为源码入口。Python 包名、命令和既有部署中的 `ashare-daily-research` 名称继续作为兼容标识；更换仓库不要求搬动服务器目录、改名服务或重建数据库。

维护者在本机 `.local/OPERATIONS.md` 记录实际部署位置、适用服务器手册的入口、现有任务与预算、最近备份、核对时间和待处理问题；只记录凭据位置和获取方法。该文件与 `.local/deployment.json` 应在受控环境单独备份和交接，Git 克隆不会带回它们。公开仓库不包含某台主机的运行状态。

发布前从当前源码建立明确的文件清单和哈希，核对实际目标并备份需要替换的文件；更新后验证维护脚本、服务、健康接口及报告状态，失败按对应发布记录回退。保留服务器既有 `.env`、数据、报告、预算与尝试记录，避免对整个运行目录做镜像覆盖。网页可访问与最新日报生成成功是两项独立检查。

公开仓库保持独立历史，不合并旧仓库分支或复制旧 `.git`。发布包与私有配置分开保存，提交前检查实际暂存内容；不要使用强制添加把 `.local/`、真实数据或日志重新纳入版本控制。

命令参数优先于本地配置。`--deployment-config PATH` 可选择另一份本地配置；显式指定的文件不存在或必要参数缺失时会停止，不会猜测目标地址。升级工具使用 `--project` 覆盖 `project_root`，服务管理工具使用 `--project-root` 指定实际本机项目目录。服务工具默认仍取自身所在的项目，避免把打包电脑上的远程目标目录当成本机安装目录。

Nginx 工具支持 `--server-ip`、`--listen-port`。以下仅预览，不写配置、不重载服务；`--all-clients` 表示预览“无来源 IP 限制”的规则，并非要求采用该访问方式：

```bash
.venv/bin/python scripts/nginx_intranet.py preview --deployment-config .local/deployment.json --all-clients
```

维护已有入口时，本地 `server_ip` 和 `listen_port` 必须与已有配置一致。工具继续严格核对原配置、记录备份并在失败时回退，不会为了适配未知配置而放宽归属检查。改变目标 IP 或端口属于另一项部署变更，不应通过脱敏顺带执行。

## 单次运行与网页

在项目根目录预览当前日观察流程：

```bash
.venv/bin/python -m ashare_daily run-daily --config config/sector_observation_daily.json --dry-run
```

真实流程需先核对来源权限、日期和预算，具体命令见使用指南。需要新浪压缩历史解码时，可在已有 Node.js 的环境使用 `PATH` 中的 `node`，或使用项目提供的 Linux x64 安装工具：

```bash
.venv/bin/python scripts/install_node_runtime.py --help
```

该工具根据 `config/node_runtime.json` 的固定版本与哈希在项目内安装运行时，需要联网下载，不是离线演示步骤。

只读网页监听本机：

```bash
.venv/bin/python -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
```

从自己的电脑连接远程主机时，可以使用已有 SSH 账号转发回环端口。把下例的 `YOUR_SSH_HOST` 替换为自己的主机或 SSH 别名：

```bash
ssh -N -L 127.0.0.1:8501:127.0.0.1:8501 YOUR_SSH_HOST
```

随后在本机访问 <http://127.0.0.1:8501>。网页只读已有产物，不能用网页服务启动成功代替日报验收。

## 定时运行

自动日任务应显式使用项目虚拟环境、工作目录、北京时间和当前观察配置。任务命令为：

```bash
.venv/bin/python -m ashare_daily run-daily --config config/sector_observation_daily.json --scheduled
```

它可能请求行情、已启用资料源和模型。21:00 是当前配置的开始时间，不保证报告立即完成。自动执行要保留跨进程锁、当日尝试记录与模型预算；同一生产任务不要在多台主机重复启用。

**现有服务工具保留原来的日任务入口。** `scripts/linux_services.py` 及 `scripts/linux-service.sh` 生成的日服务仍使用无显式配置的 `run-daily --scheduled`。部署参数外置不会把这个入口自动切换为当前观察流程；安装或启用之前必须审阅账号、项目路径和 `ExecStart`，避免覆盖已有观察版服务。

可以只生成离线预览，不写入或启用 systemd 服务：

```bash
.venv/bin/python scripts/linux_services.py preview --render-only --account researcher --project-root /home/researcher/apps/ashare-daily-research
```

这里的账号和路径只是示例。实际启用 systemd、反向代理、防火墙或其他访问配置应按自己的主机情况单独处理。本次文档不表示这些步骤已在你的环境验证。

## 备份与独立恢复

备份目标必须是尚不存在的新目录。下面名称是示例，每次操作使用不同名称：

```bash
.venv/bin/python -m ashare_daily backup --destination backups/manual-001
.venv/bin/python -m ashare_daily restore --backup backups/manual-001 --destination restore_checks/manual-001
```

Windows 使用 `.venv\Scripts\python.exe`。备份包括研究数据、报告及引用材料、非敏感配置和已存在的模型预算账本；`.env` 不打包，密钥需要在新主机单独配置。恢复检查清单、文件哈希和 SQLite 完整性，拒绝覆盖已有目标。

`partial` 表示仍有未备份的引用或其他缺口，不等于完整恢复。迁移时要保留模型预算和尝试记录，不以重建空账本重置当天调用额度。旧报告和快照按原字节保留，不能为修改路径而重写历史证据。

源数据或报告可能有独立的使用和再分发限制，备份与部署包也应保留在自己的受控环境，不作为公开代码附件上传。
