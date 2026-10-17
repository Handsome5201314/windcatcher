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

| 工具 | 作用 |
|---|---|
| `service_status` | 服务及队列健康状态 |
| `list_cfd_cases` | 分页列出匿名病例与阶段状态 |
| `inspect_cfd_case` | 准备状态、阻断原因、鼻塞当前版本/审核状态 |
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

当前入口覆盖**已准备算例的批量求解与结果读取**。从原始 CT 到自动临床交付尚未实现：导入、分割修正、解剖端面和气道确认仍走原界面，鼻塞上传/摆位/审核也使用原闭环。MCP 不提供批准审核、删除病例、任意路径读写或 shell 执行。

## 数据与恢复

`.local-automation/jobs.sqlite3` 保存调度状态、输入快照、结果摘要和状态事件，已排除 Git。几何、计算场及鼻塞账本沿用 `cases/`。排队期间允许调整病例，执行前重新核对审核、版本和输入指纹，变化则阻断。同一批量可以按顺序安排同一病例基准及鼻塞方案，鼻塞比较绑定执行时的基准结果。

客户端关闭后，已提交任务继续执行。服务重启恢复未开始项；中断的运行项标失败，要求显式重试。备份需同时保留队列和病例目录。该队列支持现有**单进程服务**，勿启动多 uvicorn worker 或第二个实例共用目录；直接 CLI 不参与 HTTP 互斥。

取消只作用于未开始项，不承诺停止运行中计算。执行期间的病例写锁覆盖网页修改；后处理和鼻塞任务仍复用原互斥约束。

## 每例 token 怎么算

无法从病例大小换算固定 token。OpenFOAM、分割、网格和本地后处理不调用大模型；AI 调度、工具定义及返回内容、解读与重试才进入调用端上下文。按 `poll_after_seconds` 查询，避免让大模型每秒轮询。

本服务不知道 Codex / WorkBuddy 的全部上下文、缓存和推理用量，返回 `llm_tokens: null`，需从宿主 usage / 账单采集。若使用网页内置 AI 报告 API，报告 `usage` 保存供应商实际返回的输入、输出、总量、缓存与推理 token，缺失时标不可用；仅覆盖报告调用，不包含外部 MCP 宿主用量。

首批用同一模型、同一提示词跑若干病例，记录宿主输入/输出/缓存/推理用量、病例数、重试、耗时和闸门是否通过，再给出每例中位数与范围。按供应商实际模型价格计算费用，订阅额度不能直接当 API 单价换算。

可给 AI 的提示词：

> 使用 windcatcher MCP 检查服务并列出病例，核对我指定病例的准备状态。只提交已确认且已准备的基准和已审核鼻塞当前版本，按顺序批量求解；需要人工确认时列明原因。返回 batch_id，按建议间隔查询；完成后汇总数值闸门、压降、质量守恒及鼻塞前后变化。保留未通过项，不把最低压降直接等同于最适合患者。另从客户端 usage 记录本批实际 token，并区分 CFD 耗时。
