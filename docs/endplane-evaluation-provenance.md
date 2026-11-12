# 端面评测清单的冻结与来源记录

端面修订账本中的 `committed` 只表示保存成功。真实病例进入离线评测前，负责人须在工作台之外核对该病例的评测授权、人工参考端面及对应修订，再手工编制 manifest 和 provenance sidecar。脚本不会从病例库、患者分享、CFD 结果或操作日志自动生成这些文件。

两份 JSON 文件都放在非公开、非 Git 的受控目录。只写匿名病例编号和内部证据编号；不要写患者姓名、病历文本、CT 路径、自由文本备注或密钥。manifest 的 `schema_version` 为 1，`cases` 每项包含 `case_id`、`scope`、`revision_id`、`reference_plan` 和 `tolerances`。参考端面由人工核对后填写，不能直接复制待评测修订充当答案。

sidecar 的 `schema_version` 为 1，并包含以下字段：

| 字段 | 要求 |
| --- | --- |
| `manifest_sha256` | manifest 文件原始字节的 SHA-256，小写 64 位十六进制 |
| `records` | 与 manifest 的病例、scope、revision_id 一一对应，不多不少 |
| 每条 `revision_plan_sha256` | 对应已保存修订账本中的 `plan_hash`；评测时还会重算方案哈希 |
| 每条 `evaluation_authorization_ref` | 人工核对过的离线评测授权记录编号 |
| 每条 `reference_confirmation_ref` | 人工参考端面核对记录编号 |
| 每条 `reviewer_id`、`reviewed_at_utc` | 核对人员的非身份敏感内部编号及 UTC 时间，格式 `YYYY-MM-DDTHH:MM:SSZ` |

先完成 manifest，再计算原始文件哈希，填写 sidecar：

```powershell
(Get-FileHash -Algorithm SHA256 -LiteralPath E:\windcatcher-eval\manifest-v1.json).Hash.ToLowerInvariant()
& ./.venv/Scripts/python.exe scripts/evaluate_endplanes.py E:\windcatcher-eval\manifest-v1.json --provenance E:\windcatcher-eval\provenance-v1.json
```

输出只含匿名编号、技术误差、失败原因、两份输入文件的哈希及评测代码来源哈希，不输出参考方案或证据编号。manifest 任意字节变化、sidecar 记录缺失或修订方案哈希不匹配均拒绝或标记失败。修改评测集时另存新版本文件并重新核对，保留旧版及输出用于比较。纯合成病例可单独使用 manifest，无需 sidecar；合成和真实病例不能混放。

**边界**：sidecar 中的证据编号是操作者的人工声明，脚本只检查结构和文件绑定，无法核实授权记录本身是否真实、有效或覆盖指定用途。评测通过只代表端面几何误差落在所填阈值内，不代表 CFD 数值合格、临床有效或允许模型训练。授权原件和人工核对记录仍须由项目负责人在受控系统中保存并审计。

## 冻结病例集合与改进指标

端面报告现在额外输出 `cohort_sha256`：按匿名病例编号、scope、人工参考端面和容差计算，**不含待比较的修订 ID**。同一冻结参考集的基线和候选方案应有相同 `cohort_sha256`；换病例、参考端面或容差就会变化。`manifest_sha256` 仍绑定各次运行的完整输入，二者不能互换。

`scripts/evaluate_learning_metrics.py` 读取端面报告和单独的人工指标 JSON。指标文件的 `schema_version` 为 `1`，`endplane_report_sha256` 为**端面报告文件原始字节**的 SHA-256；`cases` 必须与该报告的 `(case_id, scope, revision_id)` 一一对应。每条记录仅允许以下结构化字段，拒绝额外的患者反馈、操作日志、自由文本或身份字段：

| 字段 | 来源与含义 |
| --- | --- |
| `case_id`, `scope`, `revision_id` | 与端面报告精确匹配 |
| `miscut`, `quality_review_ref` | 人工核对的误切标签及核对记录编号；不能直接从患者反馈或几何容差失败推断 |
| `manual_seconds`, `manual_time_ref` | 人工处理耗时及计时证据编号，非负秒数 |
| `numeric_gate_ok`, `cfd_eligible`, `numeric_gate_ref` | 数值闸门和最终合格状态摘要及对应证据编号；合格必须先通过数值闸门 |
| `tokens`, `cost_usd`, `usage_ref` | 该例全部尝试的 token 数、美元费用及账单/用量记录编号；失败尝试也计入成本 |
| `mobile_attempted`, `mobile_completed`, `mobile_ref` | 手机端是否实际尝试、是否完成及测试记录编号；完成不能没有尝试 |
| `reviewer_id`, `reviewed_at_utc` | 非敏感内部审核人编号和 UTC 核对时间 |

上述记录只能由审核者在受控目录手工编制；脚本只验证结构、记录匹配和文件哈希，**不能核实证据编号对应的原件**。不要把真实病例清单、参考端面、指标文件或输出提交到 Git。患者反馈另存，不能当作误切标签或冻结评测授权。对于合成数据可单独测试机制，但不构成真实质量样本。

运行示例（先将端面报告保存到非公开目录，再填写对应指标文件）：

```powershell
& ./.venv/Scripts/python.exe scripts/evaluate_endplanes.py E:\windcatcher-eval\baseline-manifest.json --provenance E:\windcatcher-eval\baseline-provenance.json > E:\windcatcher-eval\baseline-endplanes.json
(Get-FileHash -Algorithm SHA256 -LiteralPath E:\windcatcher-eval\baseline-endplanes.json).Hash.ToLowerInvariant()
& ./.venv/Scripts/python.exe scripts/evaluate_learning_metrics.py E:\windcatcher-eval\baseline-endplanes.json E:\windcatcher-eval\baseline-quality.json > E:\windcatcher-eval\baseline-summary.json
```

摘要仅输出来源哈希、病例数和六项聚合指标：人工误切率、平均人工秒数、数值闸门通过率、每合格病例 token/美元费用、手机端完成率。每项均有 `value`、`numerator`、`denominator`；合格病例或手机尝试数为零时 `value` 是 `null`，不能拿来宣称提升。`dataset_sha256` 是本次指标文件的哈希，基线和候选通常不同；用 `cohort_sha256` 判断参考病例集合是否相同。摘要的 `source_references_verified=false` 始终提示来源原件尚待人工核验。端面修订缺失、哈希错误或非容差类完整性失败都会阻断指标汇总。

## 从候选反馈生成冻结样本

`scripts/export_endplane_learning_dataset.py` 是候选闭环到离线评测之间的显式闸门。调用时必须逐个提供账本生成的 proposal ID（或一个明确的 JSON ID 数组）以及来源资料的 `source_cohort_sha256`；脚本不会扫描 `committed` 修订，也不会从病例目录寻找样本。输出的 `cohort_sha256` 由实际导出的匿名病例、scope、掩膜版本和人工复核后的有效方案计算，不能由调用方随意指定。

只有账本中已有一条终态人工反馈的候选才可导出：`accepted` 使用候选方案，`adjusted` 使用反馈中结构化的人工修正版；未反馈和 `rejected` 都会拒绝整个导出请求。同一病例、scope、掩膜版本只允许一个参考方案。每条导出记录包含候选方案哈希、有效方案哈希、反馈事件哈希和病例/范围的匿名元数据，不包含 CT、掩膜本体、文件路径、患者文本或账号标识。反馈事件哈希也绑定审核者的内部 actor，但不导出该标识。导出的 `manifest_sha256` 绑定整个 JSON，`proposal_allowlist_sha256` 绑定本次显式名单。输出文件已存在时拒绝覆盖。

真实病例额外要求 `--review-references-file`：JSON 对象以 proposal ID 为键，每项精确包含 `evaluation_authorization_ref`、`reference_confirmation_ref`、`reviewer_id`、`reviewed_at_utc`（`YYYY-MM-DDTHH:MM:SSZ`）。它们必须是负责人已核对的授权和参考端面证据编号，不能放姓名、路径或自由文本。输出仅保留这些字段整体的 `review_references_sha256`，不复制证据编号或审核者标识；原文件留在受控目录。脚本仍不能验证证据原件，输出始终标记 `source_references_verified=false`。

示例（在非公开受控目录执行）：

`--actor-id` 指原提案者的内部 actor ID；服务提案经其他人工账号复核后，仍按原服务提案者导出。反馈事件哈希会绑定实际复核者。该命令运行在本机受控环境，不是对网页账号授予额外导出权限。

```powershell
& ./.venv/Scripts/python.exe scripts/export_endplane_learning_dataset.py `
  --proposal-id proposal-0123456789abcdef0123456789abcdef `
  --source-cohort-sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef `
  --actor-id operator-1 `
  --learning-dir E:\windcatcher-learning `
  --output E:\windcatcher-eval\endplane-learning-v1.json
```

该文件只是人工复核后的匿名结构化样本，不等于已训练模型或已批准的自动放行规则。它没有直接接入 `evaluate_endplanes.py` 的修订型 manifest/provenance 契约；后续仍需人工核对参考计划、配置容差并生成相应评测输入，再完成独立评测和升级门禁。导出的 cohort 哈希不含评测容差，不能替代 `evaluate_endplanes.py` 报告中的评测 cohort 哈希。

## 独立候选快照的离线评测

`scripts/evaluate_endplane_learning.py` 可以直接评测上节导出的冻结学习样本。它只读取命令中指定的冻结 JSON 和另一份候选 JSON，不打开病例目录、端面修订或学习数据库，也不会从参考方案自动生成候选。候选应由待测模型或算法独立运行后保存，精确覆盖冻结集合中的每个 `(case_id, scope, mask_version)` 一次。

候选文件结构如下；实际使用时由待测流程填写 `plan`，不要把参考答案当作待测结果：

```json
{
  "schema_version": 1,
  "frozen_manifest_sha256": "冻结文件中 manifest_sha256 的值",
  "candidates": [
    {
      "case_id": "synthetic-example",
      "scope": "main_airway",
      "mask_version": 3,
      "plan": {
        "format": "oblique_planes_v1",
        "scope": "main_airway",
        "mask_version": 3,
        "anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, -1, 0]},
        "posterior_plane": {"point_lps_mm": [0, 20, 0], "normal_lps": [0, 1, 0]}
      }
    }
  ]
}
```

使用统一的非负容差运行；法向容差单位为度，范围为 0 到 180。冻结集合含 `oblique_local_inlet_v2` 时必须提供 `--max-seed-mm`，其余情况不提供：

```powershell
& ./.venv/Scripts/python.exe scripts/evaluate_endplane_learning.py `
  E:\windcatcher-eval\endplane-learning-v1.json `
  E:\windcatcher-eval\candidate-v2.json `
  --max-plane-offset-mm 1 --max-normal-deg 5 --max-seed-mm 1 `
  > E:\windcatcher-eval\candidate-v2-report.json
```

评测会重算冻结 manifest、实际参考 cohort、proposal allowlist 和每条有效方案的哈希；真实病例还必须有审核记录哈希。它拒绝未接受/未修正参考、未知字段、缺项、重复项、版本或格式不一致，以及重复 JSON 键。输出不含几何计划、证据编号或操作者标识，仅含匿名病例的偏移/法向/种子误差与通过状态、计数和来源哈希。候选快照另有规范 JSON 哈希及原始文件哈希；`evaluation_cohort_sha256` 把参考方案与本次容差绑定，修改容差后不能冒充同一次冻结评测。

报告始终设置 `source_references_verified=false`、`auto_promotion=false`。合成样本只标记 `synthetic_only`；机制测试通过不代表真实病例切面效果提升。该报告目前是独立的候选几何评测结果，尚未转换为六项改进指标或自动驱动升级门禁。
