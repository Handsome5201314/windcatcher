# 个人账号与病例授权：备份、恢复和回退边界

启用 `WINDCATCHER_MULTIUSER=1` 后，病例目录会留下 `cases/.multiuser-identity-enabled` 标记；账号、密码哈希、可撤销会话、病例授权、上传归属及已删除病例墓碑保存在 `identity.sqlite3`。这两处必须作为同一部署的数据资产保留。本轮目标是 Windows 本机工作台经 SSH 反向隧道提供公网访问：病例仍位于工作台 `cases/`，不得在更新代码时删除或替换；身份库须在本机 `.env` 中明确指定持久的 `WINDCATCHER_IDENTITY_DIR`。魔搭容器的 `/data` 挂载是另一部署形态，不是本轮发布条件。

若病例标记仍在而身份库丢失，工作台会拒绝启动，不能把旧病例重新授给共享管理员。已启用个人账号的工作台也拒绝切回 `WINDCATCHER_MULTIUSER=0`；恢复应优先找回原身份库，不要移除标记来绕过检查。

## 在线备份

从持有身份库目录的受控主机或挂载卷执行；每次使用新文件名。示例目录仅为占位路径：

```powershell
$env:WINDCATCHER_IDENTITY_DIR = "E:\windcatcher-data\identity"
.\.venv\Scripts\python.exe scripts\backup_identity.py backup E:\windcatcher-backups\identity-20261102.sqlite3
```

脚本通过 SQLite 在线备份 API 包含已提交的 WAL 数据，验证数据库完整性、表结构和账号、授权、墓碑计数；返回 `status: verified` 才算成功。目标已存在时拒绝覆盖。备份**包含密码哈希和会话哈希**，即使输出只打印计数，也应限制备份文件和目录的访问权限，禁止放进 Git、应用镜像或公网下载目录。配套备份病例目录、患者分享和端面修订记录时，应记录同一时间点，恢复后人工核对关联版本。

## 隔离恢复与正式恢复

恢复前停止服务，保留原目录供回滚。先将快照恢复到**新的**目录；目标文件必须命名为 `identity.sqlite3`，且文件及 `-wal` / `-shm` 侧车都不存在：

```powershell
.\.venv\Scripts\python.exe scripts\backup_identity.py restore E:\windcatcher-backups\identity-20261102.sqlite3 E:\windcatcher-data\identity-restored\identity.sqlite3
$env:WINDCATCHER_IDENTITY_DIR = "E:\windcatcher-data\identity-restored"
```

恢复工具会撤销快照中的所有活动会话；用户需重新登录。核对 `status: verified`、schema 版本、账号/授权/墓碑计数，再在**隔离环境**验证旧病例授权和已撤回病例不可复用。最后保持病例目录标记，指向恢复目录启动正式服务。若身份快照晚于病例快照，需人工核对未匹配授权；若身份快照早于病例快照，新增病例可能不可访问，不能自动授给管理员。

## 每账号 AI 服务配置

个人账号模式下，AI 服务的 URL 和模型分别保存在 `cases/.ai-report-users/<账号 ID>.json`；与病例目录一起备份和恢复，**不包含在 `identity.sqlite3` 快照中**。配置文件不含 API 密钥。各账号提交的密钥仅留在当前服务进程内存，服务重启、换进程或恢复备份后都需本人重新填写；个人账号不会继承旧共享模式的全局配置或 `WINDCATCHER_AI_API_KEY`。因此继续维持单进程服务，不能以多个 worker 共享这些临时密钥。

个人账号使用 AI 服务前，管理员需在服务环境设置 `WINDCATCHER_AI_ALLOWED_HOSTS` 为明确允许的供应商域名，多个域名以英文逗号分隔，例如 `api.example.com,models.example.com`；不支持通配符。未配置时个人账号的模型探测和解读请求会被拒绝。个人账号仅允许这些域名的 HTTPS 标准端口；IP 地址、解析到内网地址的域名及 HTTP 重定向均被拒绝。每次请求只解析一次域名，实际连接固定到当次核验过的公网 IP，TLS 仍按原域名校验证书；个人请求不使用环境变量代理。若本机 AI 出站必须经代理，应另行设计受控出口并测试，不能靠恢复自动代理绕过上述校验。更新允许列表后，旧配置也会在下次请求时重新校验。旧共享模式的配置和调用行为保持原状。

## 个人界面偏好

个人模式的视角、显示范围和常用步骤保存在独立的 `operator-preferences.sqlite3`，默认与 `identity.sqlite3` 同目录，也可由 `WINDCATCHER_PREFERENCES_DIR` 指定。它只存账号 ID、版本和受限展示偏好，**不包含在身份库备份中**。复制单个数据库文件可能漏掉 WAL 中已提交的偏好，请使用独立的 SQLite 在线备份工具：

```powershell
$env:WINDCATCHER_PREFERENCES_DIR = "E:\windcatcher-data\identity"
.\.venv\Scripts\python.exe scripts\backup_operator_preferences.py backup E:\windcatcher-backups\operator-preferences-20261102.sqlite3
```

脚本验证 SQLite `integrity_check`、schema 版本、限定表结构、账号 ID 与每条偏好内容，只输出版本和记录数，不输出账号 ID 或偏好值；目标文件或 `-wal` / `-shm` 已存在时拒绝覆盖。备份文件仍属于个人配置数据，应放在受控目录，不放入 Git、镜像或公网目录。

恢复前停止服务、保留现有身份库和偏好库以便回滚。先恢复到**新的隔离目录**，目标文件名必须为 `operator-preferences.sqlite3`；脚本禁止直接写入当前配置的偏好目录：

```powershell
.\.venv\Scripts\python.exe scripts\backup_operator_preferences.py restore E:\windcatcher-backups\operator-preferences-20261102.sqlite3 E:\windcatcher-data\preferences-restored\operator-preferences.sqlite3
```

核对 `status: verified`、schema 版本和记录数；在隔离环境用对应版本的 `identity.sqlite3` 验证账号偏好归属后，再以维护窗口切换 `WINDCATCHER_PREFERENCES_DIR` 并重启。此工具只恢复偏好库，不创建账号、不合并现有偏好，也不更改病例、会话或授权；若两份快照时间点不同，须先人工核对账号关联。

私有操作配方草案另存于 `operator-recipes.sqlite3`，不包含在身份库或偏好库快照中。使用 [配方草案的独立在线备份与隔离恢复](operator-recipes.md#备份与隔离恢复)，并在恢复后核对同一批账号的归属；草案只保存建议，不执行工具。

## 操作元数据

操作日志位于 `WINDCATCHER_MONITOR_DIR`，保存匿名病例编号和服务端账号 ID，不存 CT、病历原文或输入文本。仅在 `WINDCATCHER_MULTIUSER=1` 时，操作事件按服务端 UTC 记录时间保留 180 天；写入、查询、读取状态或创建操作日志备份时清理过期事件，服务启动后及每小时还会主动清理并截断 SQLite WAL。失败会记录错误并在状态接口显示，随后重试；状态接口返回 `retention_days: 180`。旧共享模式和本机模式维持原有 `RETENTION_DAYS=None` 行为，不自动清理。

**旧备份不能靠清理在线库消除过期事件。** 对已知的每个受控备份目录运行只读审计：

```powershell
.\.venv\Scripts\python.exe scripts\audit_activity_backups.py E:\windcatcher-backups
```

审计逐个检查直属 `.sqlite3` 文件的完整性和事件时间，只输出文件名、计数、最早/最晚时间；`review_required` 或 `empty` 返回退出码 2。脚本不搜索其他目录、不读取 CT/身份、不修改或删除备份。必须单独列清所有历史备份位置，确认是否有超过 180 天的事件，再制定逐个受控清理或重做快照的操作和恢复演练；在完成前不能声称备份也满足 180 天保留要求。身份库备份不包含操作日志；操作日志仍需单独限权备份。

本机守护器可从 `.env` 传递 `WINDCATCHER_MONITOR_DIR`、`WINDCATCHER_LEARNING_DIR`、`WINDCATCHER_AUTOMATION_DIR`、`WINDCATCHER_SHARE_DIR`、`WINDCATCHER_PREFERENCES_DIR` 和 `WINDCATCHER_RECIPES_DIR`；未配置时使用代码既有默认目录。变更目录前须先备份原目录并在隔离目录核验恢复，不能仅改路径后启动空库。
