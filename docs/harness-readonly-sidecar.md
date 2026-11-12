# DeepSeek Harness 只读建议试点

Windcatcher 已增加官方 DeepSeek Harness Python SDK 的适配代码和工作台手动入口。个人账号先通过病例查看权限检查，服务端再提取八个固定阶段状态、三个质量布尔值及本机状态指纹。服务端仅向一次性专用容器发送严格白名单摘要；模型只接收阶段状态和质量布尔值，不接收匿名病例编号、指纹、CT、患者身份、病历正文、文件路径、病例原始 JSON 或 MCP 原始响应。建议只返回固定代码，不能自动执行、判定诊疗结果或放行患者分享。

当前代码默认关闭。隔离适配器使用 DeepSeek Harness 开源框架，模型路由固定为 `windcatcher-relay` / `gpt-6.1-sol`，OpenAI 兼容接口固定为 `https://tongyimohe.aitoken.credit/v1`。已完成合成测试、官方 0.2.2-alpha.2 运行时的组合配置核验；中转站 Chat Completions 非流式与流式合成探针返回 200。2026-11-04 的隔离容器纯合成端到端调用成功，source=deepseek-harness、metering=reported；尚未完成冻结病例收益比较。因此不能把这个入口说成已经具备自动建模或自进化能力。

## 输入与输出

输入白名单：schema_version=1、匿名 case_id、八个阶段状态、baseline_ready、numeric_ok、eligible、state_sha256。阶段状态只能是 pending/running/ok/warn/failed/blocked；质量字段只能为布尔或 null。没有可信的统一单调病例版本，因此不伪造 case_version。state_sha256 只反映状态投影，不能代表 CT 或 CFD 产物的完整版本。模型返回后，服务端重新核验当前账号的病例查看权和该指纹；撤权或状态变化则拒绝返回旧建议。

输入最多 4096 字节，拒绝重复键、未知字段和自由文本。模型输出只能是下一步代码、失败复盘代码和技能草案代码，必须标明人工复核且不得要求自动动作。技能草案不会自动保存或执行。只有 SDK 模型调用成功且输出通过校验，服务端才会标记 source=deepseek-harness；旧的离线规则样本 scripts/harness_readonly_sidecar.py 不会冒充模型结果。

## 官方框架与运行边界

官方 sdk-minimal 默认带 PowerShell/shell、图片读取、工作目录工具及 danger-full-access 文件策略，不能原样用于本项目。deploy/harness-readonly.patch.yml 禁用这些工具、MCP、终端与子进程服务，并改为 read-only；同时禁用官方 `llm-deepseek` 插件，新增 `dsh-llm-pi-ai` 的 `openai-completions` provider。scripts/check_harness_profile.py 在每次调用前核验组合后的配置、插件清单、固定运行时版本以及精确模型路由；任何配置漂移都会失败。Python SDK 和 npm 运行时均对应官方 0.2.2-alpha.2 标签（提交 f3803c96974ad4a94195fb97e5c637e9c2a3afe3）；先前较新 SDK 的 session/query 与固定 JS 运行时不兼容，现已对齐。这是用户指定的开源框架，不依赖本机的其他 DeepSeek 程序或本地模型。

网关已移除同 Windows 账号的 Python 子进程调用路径。新的调用路径只接受本地镜像的不可变 `sha256:` ID，使用一次性 Docker 容器、非 root 身份、只读根文件系统、受限 tmpfs、无病例/宿主目录挂载、无 Docker socket、最小权限与内存/CPU/进程数限额。摘要和专用密钥只通过 stdin 送进容器，不作为 Docker 参数或镜像内容；超时强制移除指定容器。默认仍固定 `--network=none`；只有显式提供两个出站 ID 且实时核验隔离网络和代理身份、镜像、权限与拓扑后，才接入受控内网。试点总开关仍默认关闭。

专用镜像由 `deploy/harness-worker/Dockerfile.harness` 构建，其相邻 `.dockerignore` 只放行固定的 worker 脚本、只读补丁和锁定依赖。构建时从官方 GitHub 检出完整提交 `f3803c96974ad4a94195fb97e5c637e9c2a3afe3`；npm lock 固定 `@deepseek-ai/dsh@0.2.2-alpha.2`。从项目根目录执行：

```powershell
docker build -f deploy/harness-worker/Dockerfile.harness -t windcatcher-harness-worker:f3803c9 .
docker image inspect windcatcher-harness-worker:f3803c9 --format '{{.Id}}'
```

第二条输出的本地 `sha256:` 镜像 ID 才能作为 `WINDCATCHER_HARNESS_IMAGE`；不要使用可变 tag 或沿用旧镜像 ID。固定版 Node 原生插件默认把二进制复制到临时缓存，与容器的 `/tmp:noexec` 冲突；镜像设置 `NARB_DISABLE_NATIVE_CACHE=1`，直接从只读镜像路径加载。2026-11-04 重建后专用容器以 UID 10001 运行，组合 profile 核验为 `verified tool-free`，无密钥 SDK 初始化通过。这不代表真实模型调用成功。

### 仅供验收的隔离网络探针

下列脚本是 2026-11-03 官方 API 路由的纯合成负向隔离探针，保留用于回归网络边界；当前模型路由已改为中转站，**不要把它的结果当作当前 provider 验收**。脚本在 `finally` 中只清理本次创建的容器与网络：

```powershell
$image = docker image inspect windcatcher-harness-worker:f3803c9 --format '{{.Id}}'
.\scripts\Probe-HarnessEgress.ps1 -Image $image
```

2026-11-03 在 Docker Desktop 29.9.2 的 Linux 引擎上，临时网络使用 `--internal` 和 `com.docker.network.bridge.gateway_mode_ipv4=isolated`；worker 对公网 IP、宿主 IP、代表性内网 IP 均返回 `ENETUNREACH`，官方域名与 `host.docker.internal` 在该网络无法解析，`all_blocked=true`。临时代理只在该网络监听 3128，没有发布宿主端口；它对完全匹配的 `CONNECT api.deepseek.com:443` 返回 **502**（故意不外联），对其他域名、IPv4/IPv6 字面量、大小写/尾点/用户信息变形、错误端口、Host 不匹配及普通 HTTP 请求均返回 **403**，10/10 探针通过。固定提交的 SDK 使用显式 `HTTPS_PROXY`，探针代理记录到官方 API 的 CONNECT；假密钥请求因故意的 502 返回 `JsonRpcError`。探针运行后没有残留探针容器或网络。

上述 403/502 代理只是隔离探针，从未与官方 API 建立真实上游连接；它不会被网关使用。下面的专用出站代理是另一份实现。

### 专用 CONNECT 出站代理（默认不运行）

`deploy/harness-egress/proxy.js` 每次启动只接受一个固定目标：代理程序默认 `api.deepseek.com`，或显式指定 `tongyimohe.aitoken.credit`；**当前 worker 固定使用中转站，启动代理时必须显式选择后者**。CONNECT authority、Host 和 TLS ClientHello SNI 必须与选定目标精确一致；其他域名、IP 字面量、大小写/尾点/用户信息变形、错误端口、重复 Host 及普通 HTTP 均拒绝。代理不解密模型流量，不记录请求内容、病例摘要或密钥；只记录固定事件代码。每个隧道有 DNS、连接、空闲、总时限、双向字节上限，并发最多 2。解析结果只接受公网 IPv4，再把选中的 IP 固定用于上游 TCP；工作容器继续对选定目标做 TLS 证书校验。

**每次运行只有两个出站目的地**：代理固定向 Cloudflare `1.1.1.1` 发起经 TLS 验证的 DoH 查询，查询内容只有本次选定的固定目标域名，不包含病例摘要或密钥；随后仅对该域名经公网地址校验得到的 IP 建立 443 隧道。本机 YouTu/TUN 将系统 DNS 和指定 UDP DNS 的回答改写为 `198.18.0.174`（非公网），代理会拒绝该地址，不设 fake-IP 例外。Cloudflare DoH 的额外网络依赖若不可用也会失败关闭。

从项目根目录构建并读取本机不可变镜像 ID。下面的中转站目标示例只准备隔离出站；启动前仍需独立确认中转站支持 Harness 所需协议和模型：

```powershell
docker build -f deploy/harness-egress/Dockerfile.proxy -t windcatcher-harness-egress:local .
$proxyImage = docker image inspect windcatcher-harness-egress:local --format '{{.Id}}'
$lease = .\scripts\Start-HarnessEgressProxy.ps1 -ImageId $proxyImage -TargetHost tongyimohe.aitoken.credit | ConvertFrom-Json
# 测试完按返回的精确 ID 和容器名回收：
.\scripts\Stop-HarnessEgressProxy.ps1 -InternalNetworkId $lease.internal_network_id -OutboundNetworkId $lease.outbound_network_id -ProxyContainerName $lease.proxy_container_name
# 若只清理了一部分，仍用这三个精确参数重试；已删除的资源会跳过。
```

启动脚本只接受专用镜像的 `sha256:` ID，创建一条 `Internal=true`、`gateway_mode_ipv4=isolated` 的工作容器内网和一条独立出站网。代理双网卡，默认路由走出站网，但只在内网 IP 的 3128 监听，内部固定别名为 `windcatcher-harness-proxy`；不发布宿主端口、不挂载目录、不读取病例或密钥。脚本返回 `internal_network_id`（Docker inspect 的 64 位十六进制网络 ID）、`proxy_alias`、`proxy_port`、`proxy_container_name`、`image_id` 等；启动失败会回收本次资源，回收失败会报告残留名称。停止脚本在删除前核对标签、网络归属和成员，不会按模糊名称清理其他容器。

本机公网试点的代理容器已设置 Docker unless-stopped 重启策略；今后新建长期代理可在启动脚本加 -Persistent，默认仍为临时验收模式。Docker Desktop 未启动或代理/网络被删除时，建议接口会失败关闭，不影响主工作台。回滚试点只需从项目 .env 移除本次追加的四个 WINDCATCHER_HARNESS_ADVICE、IMAGE、EGRESS_NETWORK_ID、EGRESS_PROXY_IMAGE 配置项并运行 Restart-PublicAccess.ps1；代理及两条网络可用精确租约另行清理。

网关可选配置必须同时匹配 **`WINDCATCHER_HARNESS_EGRESS_NETWORK_ID=$lease.internal_network_id`** 和 **`WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE=$lease.image_id`**。网关只接受网络 ID，不接受可变网络名；它实时检查隔离网络、唯一运行代理、固定 `windcatcher.role=harness-egress-proxy` 标签、镜像 ID、只读文件系统、非 root、`CapDrop=ALL`、无挂载/端口映射、内部别名，并要求代理的 `WINDCATCHER_HARNESS_TARGET_HOST` 与 Harness 的 `WINDCATCHER_HARNESS_BASE_URL` 对应。未设置网络 ID 时继续使用 `--network=none`；设置网络 ID 后，代理镜像 ID 缺失、无效或核验失败均返回 503，不会退回无网络模式。代理镜像与工作容器镜像不同，每次重建后须分别查询并使用各自不可变镜像 ID。

2026-11-03 的验证：`node --test tests/harness-egress-proxy.test.cjs` 通过 9 项合成测试；`scripts/Probe-HarnessDualNetwork.ps1` 证明工作容器不能直连公网/内网/代理出站网卡，拒绝请求全为 403，代理出站网卡不监听 3128。显式加 `-ProbeOfficialTls` 后，用纯合成文本和明确假密钥通过官方 API TLS 校验，得到 401；没有成功模型调用。进一步在仅当前测试进程设置两个镜像 ID、内网 ID、假密钥和总开关时，真实 `request_advice` 网关调用按预期失败关闭为 503，worker 容器无残留，代理日志无摘要或假密钥。测试后代理与两条网络均已回收；没有修改 `.env` 或启用常驻试点。

旧的宿主 Python/DSH 配置已不再被网关使用。2026-11-04 已在本机公网工作台启用个人账号的手动只读建议试点：只有具备当前病例查看权的操作者可点击生成，调用后再次核对授权与状态指纹；患者分享页不调用 Harness。当前服务使用不可变 worker 镜像、专用密钥、精确中转站模型与地址、受控代理网络，配置缺失或漂移即返回 503。默认每账号每小时最多 5 次、最多 2 个并发、单次最长 60 秒；模型原文和密钥不进入错误响应。此试点不代表自动分割、自动诊疗或已证明建模质量收益。

2026-11-04 的纯合成验收：中转站模型列表可读，OpenAI Chat Completions 非流式与流式请求成功；Anthropic Messages 接口返回 403，故使用官方 Harness 的 OpenAI 兼容 provider。Python SDK 与 JS 运行时已对齐同一 0.2.2-alpha.2 标签；将 Harness 适配预算由 8192 改为 32768 后，隔离容器端到端探针成功返回只读建议及 reported 用量。32768 是本地 Harness 压缩预算，不是中转站公布的模型上下文上限。探针只发送合成阶段状态，未发送 CT、病历或真实病例摘要。

可从项目根目录运行 `python scripts/probe_harness_relay.py` 检查模型列表和 OpenAI Chat Completions 流式协议；追加 `--check-openai` 会再发送一次非流式合成请求，`--check-messages` 才会测试已知不通的旧协议。探针从项目 .env 读取密钥，不输出密钥、响应正文，也不跟随跨域重定向；模型请求可能产生少量 token 费用。

### 用量记录的含义

只读建议接口给已授权操作者附带受限的 `metering` 元数据，不向患者分享页输出，也不保存 SDK 原始事件。它仅在固定 SDK 的完整 Turn/Step/重试生命周期均可核对、每次模型尝试都有提供方用量时标为 `reported`；缺边界或缺任何尝试用量则为 `partial` / `unavailable`，汇总 token 字段为 `null`，不能按零用量计算。`input_tokens` 仅指**未缓存**输入；`cache_read_tokens`、`cache_write_tokens` 单独列示，缺失时仍为 `null`。`cost_usd` 始终为 `null`：SDK 没有单次调用的美元账单字段，不能将其当作零费用。当前元数据只随操作者响应返回，**尚未建立持久化计费台账**；冻结评测的费用指标仍需核对过的账单或注明价格版本的估算来源。

下一步在取得授权且人工参考已核对的冻结病例上，比较无 Harness 与有 Harness 的人工耗时、错误建议、token/合格病例、恢复效果和费用，并核对账单或注明价格版本；cost_usd 为 null 不能解释为零。试点在收益和权限、质量回归明确之前保持手动只读，不扩展到自动选端或患者页面。模型不能使用现有完整 Windcatcher MCP，因为其中含提交和取消任务的写工具。

2026-11-03 补强：网关现在拒绝隔离内网中的未知容器、代理多余网卡、异常启动命令和非专用出站网络；允许已核验的并发 worker。容器删除失败后须由 Docker 精确列表确认已不存在，不能仅凭 Docker 守护进程在线就当作清理成功。停止脚本依赖租约返回的两个不可变网络 ID 和代理名，部分清理后可用原租约重试。真实临时 Docker 的正常、重复、错误 ID 拒绝及部分清理重试均已通过；完整双网负向探针通过，无测试资源残留。上述核验用于防误配与漂移；拥有宿主 Docker 管理权限的人仍是受信任的管理边界。

工作台现在显示 SDK 事件报告的本次 token 用量；若事件不完整则明确显示未知，费用仍待账单核对。该展示不等于模型输出质量或临床有效性已验收。

官方依据：
- https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/README.md
- https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/SAFETY.md
- https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/python/sdk/README.md
- https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/bundle/sdk-minimal/README.md

## 2026-11-04 端面候选学习切片

本机工作台新增独立的候选/人工反馈账本及模型无关 MCP 工具 `inspect_endplane_learning`、`propose_endplanes`。任何支持这些工具的宿主模型可提交**当前病例、当前掩膜版本**的显式结构化切面方案。服务只验证结构和版本，将其记录为待复核候选；不会复制其他病例坐标、自动保存正式端面、自动审核或自动放行 CFD/分享。不传方案的旧接口仍明确返回 `no_registered_candidate_provider`。

这两项工具属于本机工作台通用 MCP；本篇的隔离 DeepSeek Harness 只读容器仍没有 MCP 或几何输入权限，**没有因新增接口而自动获得工具**。本轮没有改变容器 profile、启用收费模型调用或部署公网。后续若要让容器生成几何候选，须单独定义其输入数据与工具边界，并在冻结评测集上验收，不能把已有状态建议入口称为自动选端面。

人工在工作台端面面板内查看候选、加载到草稿、勾选完成核对后给出接受/修正/否决。实际审核者与原提案者分别记录；模型端不提供人工反馈工具。候选只在已授权病例内可见；修改掩膜版本后，旧候选不能继续反馈。正式保存始终使用原端面保存按钮及几何预检。

还没有模型候选时，操作者也能开始积累：先确认气道掩膜，在原编辑器选好鼻孔端、鼻咽端及局部入口选点，再在“候选与纠错”中点击“登记当前草稿”。登记只生成待复核候选，来源为 `operator-manual / ui-v1`；继续加载该候选、对照 CT 核对并勾选确认，才可点击接受或修正。登记、反馈和正式端面保存是独立操作。接口也会检查掩膜已经清理、人工确认，未确认或旧版本返回 409；不能只依赖界面禁用按钮。

积累反馈后，负责人用显式 proposal 名单及真实病例授权/参考核对记录导出冻结文件，再对独立候选版本做离线比较。它是可复现的改进资料，不代表权重训练、技能自动升级、真实病例质量提升或 token 降低已经完成；这些收益仍需同一评测集的前后对照。

## 本机几何候选与 Harness 合成试验

工作台“候选与纠错”增加“本机生成候选”；通用 MCP 增加 `auto_propose_endplanes`。两者都调用同一病例授权接口，且只在掩膜已清理、人工确认、版本一致时运行。候选器在本机从当前主气道掩膜查找左/右鼻孔局部入口，生成双端面草稿；每条都经体素及真实网格预检才写入独立候选账本。无可靠候选时返回原因并允许手工选端。候选需要逐条加载、对照 CT、人工反馈，正式端面另行保存。这个版本没有从历史反馈自动调整参数，也没有验证真实病例的解剖准确率。

隔离 DeepSeek Harness worker 现在另有 `synthetic_endplane_candidate` 模式，只接受 `{geometry:{fixture:'bent_tube_v1'}}` 这一内置合成几何选择器和专用运行凭证；拒绝自由文本、病例坐标、路径及额外字段。模型输出必须通过双端面、局部入口 seed、法向及端点方向的严格校验，且附带 SDK 用量状态。该模式用于验证**开源 Harness 能否产生符合接口的候选**，目前不接真实病例几何，也不写病例账本；原只读状态建议模式仍独立。新增 worker 后需重建镜像并使用新的不可变镜像 ID。没有真实 API 调用成功和冻结病例评测前，不启用面向真实病例的 Harness 自动选端，也不据此宣称 token 或建模质量改善。
