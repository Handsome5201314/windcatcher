# 候选升级离线门禁（未接入生产算法）

这是一套本机运维门禁：用同一冻结人工参考病例集比较旧版与候选版，在人工审核后记录最多 10% 的稳定灰度分流决策，并在监测指标下降或出现权限、质量错误时回滚到旧版。它不调用 CFD、不修改病例、不切换当前服务的算法。当前没有已验证的新算法和生产调用适配器；数据库内出现 canary 只表示灰度计划获准，不能据此声称实际病例已灰度。

## 输入契约

先对旧版与候选版分别运行离线评测，取得两份学习指标摘要 JSON。两份摘要须为 data_kind=curated_anonymous、schema_version=1，且 cohort_sha256 与 total 相同。cohort_sha256 绑定匿名病例、人工参考端面和阈值，不包含候选修订 ID；dataset_sha256 绑定各次指标清单原始字节，因此两次运行可以不同。六项指标均须有有效的 value/numerator/denominator，缺失分母、合成数据、NaN、病例集不一致或任何指标下降均不能获准灰度。患者反馈和浏览器操作习惯不是本门禁的质量标签。

评测脚本记录的来源引用和哈希不能替代人工核实。操作者须另外检查病例用于评测的授权原件、人工参考方案、来源、候选代码版本及成本口径，并分别留下 reviewer_id、approval_ref、authorization_ref、source_review_ref。本机 CLI 仅记录这些引用，不能验证引用背后的事实，也不提供有认证的审批界面。

## 本机操作

把数据库和报告放在受限且定期备份的本机数据目录，不放入仓库、static/ 或公网目录。以下是命令格式；路径、版本及审核引用由实际项目记录填写：

    $python = ".\.venv\Scripts\python.exe"
    $cli = "scripts\manage_upgrade_gate.py"
    $db = "E:\windcatcher-data\identity\upgrade-ledger.sqlite3"
    & $python $cli --db $db register <candidate-id> <baseline-version> <candidate-version> <baseline-report.json> <candidate-report.json>
    & $python $cli --db $db approve <candidate-id> <reviewer-id> <approval-ref> <authorization-ref> <source-review-ref>
    & $python $cli --db $db start-canary <candidate-id> 5
    & $python $cli --db $db status <candidate-id>

register 有退化或无改进时保留 rejected 记录，退出码为 1；输入无效退出码为 2。approve 只能推进无退化且至少一项改善的候选，且必须分别填入评测授权、来源核对和批准记录的引用。start-canary 要求这些人工记录，并且同一数据库同时只能有一个灰度候选。CLI 会拒绝把门禁库建在仓库内。assignment <candidate-id> <anonymous-case-id> 可检查稳定的版本分流；它不读取或持久保存病例编号，调用方仍须先鉴权，且当前工作台未使用其结果。

接入真实灰度前，需由独立、受审查的算法适配器消费分流决策，明确旧版和候选版的不可变构建标识，验证病例权限，再完成两版计算。不得让 CLI 或使用习惯配方直接运行 CFD。实际灰度监测用新病例集的成对摘要：

    & $python $cli --db $db observe <candidate-id> <new-baseline-report.json> <new-candidate-report.json>
    & $python $cli --db $db incident <candidate-id> permission_error <incident-ref>
    & $python $cli --db $db rollback <candidate-id> <rollback-ref>

监测摘要重放冻结评测集、无法读取、格式错误或来源病例集不一致时，一律停止灰度；任一六指标下降，或有权限/质量错误，也立即把状态置为 rolled_back、分流比例清零。incident 可不等指标报告而立即回滚。状态转移和证据哈希存入 SQLite 事件表；不存 CT、患者身份、端面方案或完整报告。正式上线前，还要验证备份/恢复、Windows 本机与公网反代运行时权限、人工病例审核和实际算法回滚演练。
