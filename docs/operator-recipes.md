# 私有操作配方草案存储契约

pipeline.operator_recipes.OperatorRecipesStore 只存储个人配方草案，不执行工具、不监听触发条件、不读取病例、不采纳操作日志，也不授予任何病例权限。个人模式下有经会话鉴权的草案 API；它仍不是执行器。

## 数据边界

- 数据库：operator-recipes.sqlite3，默认与身份/偏好数据库同目录；可用 WINDCATCHER_RECIPES_DIR 指定独立目录。
- owner_id：由已认证服务端会话注入的 32 位小写十六进制账号 ID，绝不可接受请求体自报的账号 ID。
- recipe_id：服务端生成的 recipe- 加 32 位小写十六进制随机 ID。
- version：每次更新或撤销递增；revoked 永久保留，撤销后不可编辑或复活。列表保留撤销条目，便于显示历史状态。
- spec 只含 suggested_steps（1–8 个唯一枚举）、allowed_tools（1–4 个唯一枚举）和 trigger_condition（单个枚举）。不存患者 ID、CT、自由文本、参数、病例日志或密钥。

当前只读工具枚举为 service_status、list_cfd_cases、inspect_cfd_case、get_cfd_batch；建议步骤枚举为 qc、segment、endplanes、surface、cfd_prep、cfd_results、report、review；触发条件枚举为 manual、case_stage_changed、batch_finished、numeric_gate_failed。这些枚举只是草案标签，不构成执行授权。submit_cfd_batch、cancel_queued_cfd、cfd_run、share 不在枚举内。

## 个人草案 API

个人模式开放 GET/POST /api/operator/recipes、GET/PUT /api/operator/recipes/{recipe_id} 和 POST /api/operator/recipes/{recipe_id}/revoke。写入只接受 spec 和必要的 expected_version；网关从已认证会话取得 owner_id，禁止请求体自报。GET 对不存在或其他账号的配方均返回 404；更新和撤销对两种情况也同为 404，不暴露跨账号版本。版本不匹配返回 409，已撤销草案不可再更新。共享账号模式不开放这些路由。

未来若要实际调用只读 MCP 工具，需另外建立独立最小权限执行器和逐病例授权过滤；此处的 allowed_tools 不能直接传给代理作为权限或绕开当前 MCP 服务身份的授权边界。

## 使用习惯线索（按需分析）

个人模式的 GET /api/operator/recipe-candidates 只在本人点击“分析我的操作记录”后读取。入口仅检查服务端已绑定到该账号的近 180 天成功操作事件，并再次过滤当前仍有查看权限、未处于删除中的病例。每个相同的有序步骤组合必须出现在至少 3 个不同病例中，且只返回步骤枚举和病例计数；最多检查最近 2000 条成功阶段事件、展示 3 条候选。当前可从事件可靠识别的步骤仅为 qc、segment、surface、cfd_prep、report，不把 cfd_run 或端面编辑推断成复核完成。

浏览器事件可以由操作者自行触发或伪造，因此这里仅是**使用习惯线索**，不是建模质量、临床有效性或自动化训练标签。候选没有病例编号、工具权限、参数或自由文本，也不会进入配方库。点击“填入草案”只预填建议步骤；本人还需选择只读信息、核对建议时机并单独点击“创建草案”才能保存。保存后仍不执行任何工具。

## 备份与隔离恢复

运行 python scripts/backup_operator_recipes.py backup <新备份文件> 会使用 SQLite 在线备份获取已提交的 WAL 状态；备份前后校验完整性、版本、精确表/索引结构、账号和配方 ID、记录版本、撤销状态及无重复 JSON 键的 spec。输出只含 schema_version 和记录数，不输出账号、病例或配方内容。目标文件及其 WAL/SHM 侧文件已存在时拒绝覆盖。

运行 python scripts/backup_operator_recipes.py restore <备份文件> <隔离目录/operator-recipes.sqlite3> 只会创建新文件，不合并已有数据，也不会写入当前在线库或其子目录。先在隔离目录验收，再停服务、备份当前库，并由运维按变更流程置换；恢复命令本身不执行上线置换。备份文件包含个人偏好以外的配方元数据，应放在受保护位置，不放进公网静态目录或版本库。
