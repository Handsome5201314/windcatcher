# 参考病例库

## 使用入口

登录公网研究工作台 → 左栏「参考病例」。列表直接读取工作台已有病例，不需要复制 CT 或重新导入。

1. 选择对应病例，填写对外展示标题和说明，不填写患者身份信息。
2. 勾选团队已核对的标签。结构特征、临床诊断、术后状态是不同维度，可多选；应用不会根据模型自动猜标签。
3. 选择「仅结构」「结构＋气流（正式参考）」或「结构＋气流（未复核测试）」，登记本次审核记录人、说明，确认该病例的展示授权和标签对应关系。测试气流需额外确认可在无需登录的参考库展示 24 小时。
4. 保存草稿不会对外展示；点击「核对并发布参考」才生成固定版本。
5. 「打开参考病例库」进入独立页面 /references。患者的原有分享页也有「与参考病例对照」入口。
6. 点击「撤回参考」后，公开清单不再显示该记录，旧模型地址拒绝访问。删除源病例也同步撤回。

工作台已有四个病例均已核对结构模型可导出。它们仅作为管理候选，不自动生成临床标签或发布记录。用户已确认有展示授权及人工标签；具体病例到标签的对应关系由管理员在上述表单登记。

## 模型与发布条件

- 结构参考读取当前已确认掩膜对应、surface 阶段为 ok 的气道 STL。坐标从毫米转换为米，为浏览器显示减至约 20,000 个三角面；快照记录原始／展示面数。原始 STL 和计算网格不修改。
- 结构模型只有坐标和表面，不填充虚构的速度、压力或数值范围。表面是气道空气空间，不是鼻甲、鼻中隔等组织的独立分割。
- 正式气流参考要求当前结果 ready、source_signature 有效、numeric_ok 和 eligible 均为 true，沿用已有数值／网格／工程复核，不启动求解。
- 未复核测试气流是独立发布类型：仅本机公网入口可发布，要求已确认掩膜、已完成求解且可视化结果 ready；正式复核闸门保持不变。测试条目在公开清单中标记为未复核，flow_verified 为 false，24 小时后从清单自动下线且模型接口拒绝访问；可提前撤回。页面同时提示不可作为正常参考或诊疗依据。
- 气流快照沿用 patient_shares 的模型字段白名单；发布时固定参数，按已记录密度转换运动学压力。缺少密度时保留原单位。
- 发布时比较上游输入哈希及气流版本；活动病例被拒绝。路由复用批任务、后处理和情景计算的病例修改锁。

## 数据与权限

- pipeline/reference_cases.py 是参考库的业务入口；pipeline/reference_routes.py 安装专用路由。原有 /reference-result 原始参考接口仍被阻断。
- 元数据与操作记录保存在 .local-shares/shares.sqlite3 的 reference_cases、reference_events 表中；模型是 .local-shares/reference-snapshots/ 下按 SHA-256 命名的 JSON。
- 快照先写临时文件再替换，事务内切换元数据指针。公开读取时校验 SHA-256。公开清单的 version 同时绑定模型、工况与标签，前端请求模型时携带 version；发布版本已改变返回 409，防止旧工况与新模型串用。
- 公开响应只有展示标识、标题、说明、标签、模型类型、有限工况与展示说明；不返回私有 case_id、路径、原始 CT、审核人、审核说明或患者访问码。
- /api/reference-admin/* 复用研究工作台登录与同源保护。/references、/api/references 及其已发布模型是获授权后的公开展示范围，采用 no-store、no-referrer 和严格 CSP。
- 撤回不会物理删除审计记录与内容寻址快照；它立即阻断公开读取。备份需同时包含 SQLite 和 reference-snapshots，文件受既有 .local-shares/ 忽略规则保护。

## 对照行为

- 默认「看结构」，隐藏流线、粒子、端面和切片，并恢复不透明的中性色表面。切换字段不会把结构模式意外切回气流。
- 单个已发布且已复核的气流模型可独立切换「看气流」；仅发布结构或气流数据不完整时保持禁用，并在页面说明原因。单模型色标使用自身范围。
- 两侧按各自模型中心和视口取景，同步观察方向及相对缩放；不是解剖配准或等比例测量。
- 未复核测试气流可单独查看；如与第二模型工况和单位完全一致，也可用于纯可视化双模型对照，并持续显示未复核警示。测试类型不会被标成已复核。
- 只有两侧均为已复核气流、单位一致，且 solver、turbulence_model、inlet_mode、total_flow_lpm、nu、rho_kg_m3 全部记录并一致时，允许气流对照。
- 允许气流对照时，两侧速度／压力均采用两个模型数值范围的并集作为统一色标。不满足条件时保留结构对照并解释原因，不用颜色推断正常、异常或治疗改善。
- 切换模型会取消上一请求；过期异步返回不能覆盖当前选择。关闭对照、切页时释放 WebGL 资源。空库、筛选无结果、网络错误、撤回及重试均有明确状态。

## 验证

- 后端：.venv/Scripts/python.exe -m pytest tests/test_reference_cases.py tests/test_patient_shares.py tests/test_cloud_access.py tests/test_public_activity_gateway.py tests/test_cfd_results.py tests/test_cfd_results_api.py -q --tb=short
- 前端：node --test --test-reporter=tap tests/*.test.cjs
- 浏览器：python tests/reference-browser-smoke.py；测试专用的合成 HTTP fixture，真实 Chromium/WebGL，覆盖 320、390、844、1440 宽度、结构／气流、统一色标、同步、管理发布撤回、错误重试与患者返回。
- 实际已授权结构验收：同脚本支持 --anatomy-model，仅读取去标识展示 JSON。临时模型验收后删除，不作为生产样例发布。
- 患者回归：python tests/patient-view-browser-smoke.py
- 公网：python scripts/Smoke-PublicGateway.py；python tests/reference-public-smoke.py。后者验证 HTTPS、资源哈希、研究认证、四个候选和真实手机管理页，不发布病例或修改标签。

2026-11-01：104 项相关后端测试、137 项 Node 测试通过；四个实际 STL 均成功导出，实际展示模型与合成模型均完成手机／桌面 WebGL 验收。公网登录、权限与手机管理页验收通过，待管理员登记标签后发布参考。
