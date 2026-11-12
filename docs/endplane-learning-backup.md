# 端面修正账本：备份与恢复

端面保存记录写入 SQLite 数据库 `.local-learning/learning.sqlite3`。它保存匿名病例编号、方案、掩膜版本与保存状态；`committed` 只表示方案曾成功保存，**不表示人工参考标签、训练授权、临床结论或 CFD 合格**。本备份不包含 CT、掩膜、STL、CFD 场、患者分享或独立的人工评测清单。

默认目录位于应用根目录下。长期使用时，在服务启动环境中设置 `WINDCATCHER_LEARNING_DIR` 到应用发布目录以外的持久目录，并把该目录纳入独立备份策略；重新部署或重建容器时保持同一目录挂载。账本不能通过 Git 或应用镜像恢复。

## 在线备份

服务运行中可以备份；脚本使用 SQLite 在线备份 API，包含 WAL 中已提交的记录。目标文件必须不存在，建议放在受控、非公开、非 Git 的备份目录。

```powershell
$env:WINDCATCHER_LEARNING_DIR = "E:\windcatcher-data\learning"
.\.venv\Scripts\python.exe scripts\backup_endplane_history.py backup E:\windcatcher-backups\learning-20261102.sqlite3
```

命令只在快照通过 `PRAGMA quick_check`、表结构与记录数核验后输出 `status: verified`；同时报告全部修订数与已保存修订数。备份失败会返回非零退出码。每次使用新文件名，不覆盖旧备份。

## 恢复演练或故障恢复

先停止工作台服务，避免恢复期间产生新的端面保存。恢复到**明确指定的新目录**，目标 `learning.sqlite3` 文件及其 `-wal` / `-shm` 侧车文件都必须不存在；脚本拒绝覆盖。可先在隔离目录做恢复演练：

```powershell
.\.venv\Scripts\python.exe scripts\backup_endplane_history.py restore E:\windcatcher-backups\learning-20261102.sqlite3 E:\windcatcher-data\learning-restored\learning.sqlite3
$env:WINDCATCHER_LEARNING_DIR = "E:\windcatcher-data\learning-restored"
```

检查命令的 `status: verified`、schema 版本和记录数，再让**停止中的**服务改用这个持久目录并启动；核对预期病例的端面记录和病例文件版本。不要把恢复的账本与不同时间点的病例目录当成自动匹配。原目录先保留用于回滚，不要直接覆盖或删除。若备份损坏、表结构不兼容、目标已存在或残留侧车文件，恢复会失败且不把目标作为可用账本。

评测清单及其来源记录需独立管理和授权；具体冻结流程见 [端面评测清单的冻结与来源记录](endplane-evaluation-provenance.md)。备份成功不会把任何病例自动纳入评测或训练。
