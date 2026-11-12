# 批次进度与完整点击流程验收（2026-11-05）

本轮在可见浏览器中操作。公网批次进度修复通过验收；合成 CT 的导入、分割、三端面保存、STL 和 CFD 算例生成通过。真实 OpenFOAM 求解在第 833 次迭代失败，因此完整计算流程验收不通过，未生成有效流场或患者报告。

## 部署与隔离范围

- 正式服务是本机 8765 加公网 HTTPS 反代，域名 `windcatcher.tongyimoheai.top`。
- 已通过 `scripts/Restart-PublicAccess.ps1` 重启加载 Python 修复，公网和本机健康检查通过。
- 公网页面已实测载入 `app.js`、`endplane-picker.js` 的 `20261105-batch-progress2` 版本和 `orchestration-ui.js` 的 `20261105-batch-progress3` 版本。
- 完整点击验收位于 `C:/Temp/windcatcher-ui-e2e-20261105`，只监听本机 18765。病例、队列、学习记录等目录独立。
- 合成病例 `case-20261105-121332-5eab` 始终标为 `synthetic=true`。测试用确认操作只发生在该隔离病例中。
- 公网仅重现既有五例的提交和人工闸门结果，没有代填真实病例审核。

## 本轮修复

1. 入队原本在队列锁中逐例执行大文件指纹和完整预检，页面必须等整批检查后才收到回执。编排队列现在以文件身份、大小和时间戳快速记录入队版本，执行前仍保留授权、版本和完整内容检查。
2. 队列执行过程中写入当前动作；页面约每 3 秒刷新，显示第几例、当前步骤、检查数量、已用时和最近同步时间。进度位于病例勾选列表上方。
3. 请求有 20 秒超时；提交回执即解锁状态更新，断线后保持任务运行状态并自动重连。重试沿用幂等请求编号，刷新页面可恢复批次。
4. 取消按钮改为“取消未开始病例”，对应实际行为。
5. CT 选区不可用时直接显示原因和调整办法，不再只藏在悬停提示中。掩膜确认失败原因及恢复办法保留在操作区。
6. 合成数据可进入相同的手动选面流程，不再需要临时改变病例合成标记。
7. 实点发现身体包络的闭运算在有限图像边界侵蚀组织，误将封闭内腔当作体外空气。清理和确认现在共享逐层包络函数，保留实际观察到的边缘组织。回归测试同时验证真实体外区域仍被排除，没有放宽体外空气闸门。

## 公网实际点击结果

路径：工作台 → 读取病例 → 勾选原问题中的五例 → 按顺序启动。

- 批次：`batch-a9cc1d066e856117`。
- 创建时间：2026-11-05 12:40:41 +08:00。
- 浏览器网络事件记录 POST 至 HTTP 202 约 **0.203 秒**；随后按约 3 秒间隔同步。
- 实际看到 `0/5 → 1/5（第 2 例检查中）→ 2/5（第 3 例检查中）→ 5/5`。
- 12:41:41 最后一例结束：通过 0、待复核 5、失败 0。
- 原因：一例缺三局部端面，三例导入/脱敏/质控尚未就绪，一例掩膜待复核。每例均显示原因和下一步；这些等待审核的病例未调用模型。
- 新版页面重新加载后恢复了该批次终态。

证据：

- `C:/Temp/windcatcher-ui-e2e-20261105/evidence/public-batch-running.png`
- `C:/Temp/windcatcher-ui-e2e-20261105/evidence/public-batch-progress.png`
- `C:/Temp/windcatcher-ui-e2e-20261105/evidence/public-batch-completed.png`
- `C:/Temp/windcatcher-ui-e2e-20261105/evidence/public-batch-result.json`

## 合成 CT 的实际点击路径

1. 病例 → 手动路径输入 → 合成 DICOM 目录 → 勾选合成测试与去标识 → 导入并质控。96 层、0.5 mm。
2. 开始分割 → 去除体外空气 → 在三平面检查 → 确认气道范围。
3. 端面裁切 → 展开精确 CT 层号 → 双鼻孔第 20 层 → 鼻咽第 112 层。
4. 核对右侧 CT：双鼻孔 2/2、鼻咽 1/1。三端技术预检通过，面积分别为 18.5、27、36 mm²。
5. 查看整体并拖动旋转实际裁切模型 → 确认范围并保存。界面回执为“双侧三端面已保存”。
6. 生成 STL 与质检 → 几何检查通过。
7. 生成 CFD 算例 → 双侧压力边界、16 个 MPI 子域。
8. 开始气流求解 → 页面显示网格生成、命令步骤、迭代次数和残差 → 求解失败。

## 剩余计算阻塞

- OpenFOAM 网格为 **428,617** 个单元，单连通区域，`checkMesh` 输出 `Mesh OK.`，四个边界为 wall / inlet_left / inlet_right / outlet。
- `simpleFoam` 在第 **833** 次迭代退出，退出码 **136**。日志记录 MPI rank 11 的 `Floating point exception`。
- 该轮最后速度线性求解的残差扩大至约 `5.46e3 / 2.66e3 / 1.70e4`，已有明确的数值失稳证据；现有证据尚不足以把根因归结为某一个边界条件或离散格式。
- 现场重读结果闸门：`numeric_ok=false`、`eligible=false`。未将失败标成成功，未导出可用患者流场或报告。
- 下一项计算工作应保留本次输入和失败日志，针对求解稳定性做可复现的数值修正与比较；不能通过放宽收敛阈值、补写审核或改写运行状态完成验收。

证据目录：`C:/Temp/windcatcher-ui-e2e-20261105/cases/case-20261105-121332-5eab`。

- `cfd/logs/simpleFoam.log`
- `cfd/logs/checkMesh.log`
- `cfd/run_status.json`
- `C:/Temp/windcatcher-ui-e2e-20261105/evidence/numeric-gate.json`
- `C:/Temp/windcatcher-ui-e2e-20261105/evidence/cfd-failed.png`

## 回归与回退

- 全套 Python：**1341 passed, 1 skipped**。
- 全套 Node：**275 passed**。
- 公网网关：**25 项 smoke probe 通过**，验证 HTTPS 证书和主机名。
- Python 日志：`C:/Temp/windcatcher-batch-progress-full-python.log`。
- Node 日志：`C:/Temp/windcatcher-batch-progress-node.log`。
- 发布前备份：`.local-public/backups/batch-progress-20261105/`，含本轮修改的原始应用文件及两个队列 SQLite 备份。回退代码后使用现有重启脚本，不回滚病例和审核数据。

这份记录是产品操作与工程验收记录，不是患者分析报告；合成例的结果不能证明真实病例自动处理质量。
