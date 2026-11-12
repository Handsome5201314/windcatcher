# 用 Codex / WorkBuddy 批量调用风见

调用链：`AI 客户端 → stdio MCP → 原有 127.0.0.1:8765 服务 → SQLite 队列 → OpenFOAM`。适配器不启动第二个业务服务，不直接写病例文件，也不调用收费大模型。网页和原公网隧道继续使用原服务。自动化 HTTP 接口拒绝公网代理和跨站调用，不能把公网网页地址填成 MCP URL。

## 安装与验证

在 windcatcher 根目录运行（当前机器已装好独立 `.venv-mcp`）：

```powershell
.\.venv\Scripts\python.exe -m venv .venv-mcp
.\.venv-mcp\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements-mcp.txt
.\.venv-mcp\Scripts\python.exe scripts\Smoke-Mcp.py
```

原服务须先启动。安装 MCP 不会升级 CFD 环境的依赖。默认 `http://127.0.0.1:8765`；端口变化时在客户端设置环境变量 `WINDCATCHER_URL`，只支持本机 HTTP 地址。冒烟脚本用官方 SDK 验证初始化、工具发现、真实状态查询、参数及错误传播，不提交计算。

## Codex

官方 OpenAI Docs：[Codex MCP](https://developers.openai.com/codex/mcp)。注册命令（移动项目后需更新路径）：

```powershell
codex mcp add windcatcher -- 'E:\AI新青年\鼻腔气流CFD比赛\开发\windcatcher\.venv-mcp\Scripts\python.exe' 'E:\AI新青年\鼻腔气流CFD比赛\开发\windcatcher\windcatcher_mcp.py'
```

也可在客户端 MCP 设置中填写同样的 command 和 args。等价 TOML：

```toml
[mcp_servers.windcatcher]
command = 'E:\AI新青年\鼻腔气流CFD比赛\开发\windcatcher\.venv-mcp\Scripts\python.exe'
args = ['E:\AI新青年\鼻腔气流CFD比赛\开发\windcatcher\windcatcher_mcp.py']
startup_timeout_sec = 15
tool_timeout_sec = 60
```

未自动改写全局客户端配置。注册后重新连接 MCP / 新开会话，检查工具是否出现。

## WorkBuddy

在支持 stdio MCP 的 WorkBuddy 版本中填写 command / args；如果界面支持 `mcpServers` JSON 导入，可用：

```json
{
  "mcpServers": {
    "windcatcher": {
      "command": "E:\\AI新青年\\鼻腔气流CFD比赛\\开发\\windcatcher\\.venv-mcp\\Scripts\\python.exe",
      "args": ["E:\\AI新青年\\鼻腔气流CFD比赛\\开发\\windcatcher\\windcatcher_mcp.py"]
    }
  }
}
```

已验证官方 MCP SDK 的 stdio 连接；未在 WorkBuddy 设置界面实测导入，菜单与导入格式以该版本为准。

## 工具与范围

公网守护器运行 `cloud:create_app` 时，MCP 通过同一个本机服务访问自动化接口。首次启用需在 `windcatcher/.env` 放入随机、仅供本机使用的 `WINDCATCHER_MCP_TOKEN`（43–128 位 base64url），重启公网守护任务；适配器从同一文件读取该密钥。公网代理、浏览器登录会话和魔搭部署均不能调用此入口。MCP 工具显示在 AI 客户端的工具列表中，不在工作台网页左栏显示。

| 工具 | 作用 |
|---|---|
| `service_status` | 服务及队列健康状态 |
| `list_cfd_cases` | 分页列出匿名病例与阶段状态 |
| `inspect_cfd_case` | 准备状态、阻断原因、鼻塞当前版本/审核状态 |
| `inspect_endplane_learning` | 读取当前操作者可见的匿名端面候选与人工反馈 |
| `propose_endplanes` | 提交当前病例结构化端面候选到独立学习账本，等待人工复核 |
| `auto_propose_endplanes` | 在本机用当前已确认掩膜生成鼻孔端/鼻咽端候选，写入待复核账本；不提交正式端面 |
| `submit_cfd_batch` | 提交 1–50 项，立即返回任务号 |
| `get_cfd_batch` | 进度、失败原因、最终指标和对比摘要 |
| `cancel_queued_cfd` | 取消未开始项；运行中求解继续 |

提交基准项（case_id 换成实际编号）：

```json
{
  "request_key": "charlie-run-001",
  "items": [{"case_id": "case-20261017-120000-abcd", "backend": "docker", "timeout_s": 7200}]
}
```

鼻塞项在对象中增加实际 `scenario_id` 和整数 `version`，两者须一起提供。人工审核必须绑定当前摆位版本。用 `inspect_cfd_case` 查询编号和审核状态。

默认串行，避免多个求解同时占满 CPU。相同幂等键及相同参数返回原任务；相同键更换参数会拒绝。失败后检查原因，用新键显式重试。`completed` 仅表示各项已结束，需看 `counts` 和每项 `status` 判断成败；`solved` 表示求解器跑完，需另外检查 `gate.numeric_ok`、`gate.eligible` 和 `comparison`。

当前入口覆盖**已准备算例的批量求解、结果读取和端面候选账本**。主气道掩膜经人工确认后，还可用本机几何算法生成待复核的双端面草稿；算法无法可靠定位或预检失败时明确返回 `unavailable`。从原始 CT 到自动临床交付尚未实现：导入、分割修正、解剖端面复核和气道确认仍走原界面，鼻塞上传/摆位/审核也使用原闭环。MCP 不提供批准审核、代替人工反馈、删除病例、任意路径读写或 shell 执行。

## 端面候选学习接口

`propose_endplanes` 使用匿名 `case_id` 和 `request`，请求字段为 `scope`、严格整数 `mask_version`、可选 `plan`、有界标识符 `source` 与 `source_version`。不再接收 `current_summary` 或调用方自报授权的 `reference_cohort`。`plan` 只接受严格白名单中的几何字段，拒绝未知字段、自由文本、文件路径、患者身份或 CT 内容；源标识仅追踪候选来源，不代表模型已通过评测。

不提供 `plan` 时只查询当前候选能力：

```json
{
  "case_id": "case-20261104-123456-abcd",
  "request": {
    "scope": "main_airway",
    "mask_version": 3,
    "source": "model_candidate",
    "source_version": "v1"
  }
}
```

不传方案的 `propose_endplanes` 仍返回 `unavailable`，不会复制其他病例的绝对坐标来制造建议。模型提供当前病例的显式结构化 `plan` 后，服务核对病例权限、掩膜版本，以及当前掩膜已清理并经人工确认的状态，只把候选写入独立学习账本；不会写入 `segment/endplanes.json`、使候选成为正式端面或放行 CFD。未确认或过期掩膜的提案与反馈返回 409。

`auto_propose_endplanes` 是另一条本机路径：只需匿名 `case_id`、`scope` 和严格整数 `mask_version`，由服务读取**该病例已确认的当前主气道掩膜和体数据空间几何**，不使用 CT 灰度作为候选特征，也不将影像发送给模型。本机候选器对左/右鼻孔分别提出局部入口方案，使用体素和真实网格预检；每侧失败独立跳过，两侧都不可靠时不登记候选。返回的候选会标注 `local_mask_geometry_left/right`（单支时为 `single`），随后仍必须在工作台逐一加载、对照 CT 人工核对。`full_candidate` 范围暂不支持本机局部入口候选。这不是基于 30–50 例的自动学习，也不表示医学正确。

接受、修正、否决属于人工反馈，必须由有 `review` 权限的操作者通过工作台或 API 完成。通用 MCP 只暴露查询和提案，不注册 `record_endplane_feedback`，模型不能代填“人工已接受”。正式保存仍走原端面操作。候选与反馈积累后还需要冻结评测、版本对照和发布门禁，不会自动训练或直接改善建模算法。

## 数据与恢复

`.local-automation/jobs.sqlite3` 保存调度状态、输入快照、结果摘要和状态事件，已排除 Git。几何、计算场及鼻塞账本沿用 `cases/`。排队期间允许调整病例，执行前重新核对审核、版本和输入指纹，变化则阻断。同一批量可以按顺序安排同一病例基准及鼻塞方案，鼻塞比较绑定执行时的基准结果。

端面候选与人工反馈单独保存在配置学习目录的 `endplane-learning.sqlite3`，默认位于 `.local-learning/`，不写入病例目录。网页/API 按病例授权查询，以便有权限的人工审核者复核服务账号提交的候选；底层个人账本方法仍按 actor 隔离。反馈分别保留原提案作者和实际审核者。本机 MCP 服务账号也须有显式病例 `view` 权限，不因持有 token 获得所有病例访问权。

公网网关只在本机回环地址、无代理/浏览器来源头且专用 token 匹配时放行学习 GET、POST `.../learning/propose` 和 POST `.../learning/auto-propose`；仍须给服务账号显式授予该病例的 `view` 权限。`.../learning/feedback` 不接受 MCP token 作为身份；人工需登录且具备该病例 `review` 权限。新工具需重新连接 MCP 后才出现在工具列表，修改源码不会热更新既有 MCP 进程。

客户端关闭后，已提交任务继续执行。服务重启恢复未开始项；中断的运行项标失败，要求显式重试。备份需同时保留队列和病例目录。该队列支持现有**单进程服务**，勿启动多 uvicorn worker 或第二个实例共用目录；直接 CLI 不参与 HTTP 互斥。

取消只作用于未开始项，不承诺停止运行中计算。执行期间的病例写锁覆盖网页修改；后处理和鼻塞任务仍复用原互斥约束。

## 每例 token 怎么算

无法从病例大小换算固定 token。OpenFOAM、分割、网格和本地后处理不调用大模型；AI 调度、工具定义及返回内容、解读与重试才进入调用端上下文。按 `poll_after_seconds` 查询，避免让大模型每秒轮询。

本服务不知道 Codex / WorkBuddy 的全部上下文、缓存和推理用量，返回 `llm_tokens: null`，需从宿主 usage / 账单采集。若使用网页内置 AI 报告 API，报告 `usage` 保存供应商实际返回的输入、输出、总量、缓存与推理 token，缺失时标不可用；仅覆盖报告调用，不包含外部 MCP 宿主用量。

首批用同一模型、同一提示词跑若干病例，记录宿主输入/输出/缓存/推理用量、病例数、重试、耗时和闸门是否通过，再给出每例中位数与范围。按供应商实际模型价格计算费用，订阅额度不能直接当 API 单价换算。

可给 AI 的提示词：

> 使用 windcatcher MCP 检查服务并列出病例，核对我指定病例的准备状态。只提交已确认且已准备的基准和已审核鼻塞当前版本，按顺序批量求解；需要人工确认时列明原因。返回 batch_id，按建议间隔查询；完成后汇总数值闸门、压降、质量守恒及鼻塞前后变化。保留未通过项，不把最低压降直接等同于最适合患者。另从客户端 usage 记录本批实际 token，并区分 CFD 耗时。
