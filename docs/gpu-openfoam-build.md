# GPU OpenFOAM 构建与验收记录

状态（2026-11-12）：**隔离 GPU 构建已越过压力场、速度场和 phi 初始化，但合成 `simpleFoam` 在 `Time = 1` 仍因 `cudaErrorIllegalAddress` 退出。GPU 求解仍不可用，未部署或开放工作台选项。** 不能把 CUDA 设备可见、编译成功或发生过 CUDA kernel 调用当成求解成功。

## 确定的输入

| 项目 | 已核实内容 |
| --- | --- |
| 显卡 | NVIDIA GeForce RTX 3090，24 GiB，compute capability 8.6（cc86） |
| 当前 CPU 镜像 | `microfluidica/openfoam:latest`，OpenFOAM v2606 GCC 构建；添加 `--gpus all` 不会使它 GPU 求解 |
| GPU OpenFOAM 源码 | [openfoam/gpu/openfoam-ecse 的 feature-gpu](https://gitlab.com/openfoam/gpu/openfoam-ecse/-/tree/feature-gpu)，提交 `559823f7`；隔离检出在 WSL ext4 的 `/home/lishuaishuai/gpu-openfoam-poc/src` |
| ThirdParty | [openfoam/core/thirdparty-common](https://gitlab.com/openfoam/core/thirdparty-common)，提交 `1eb9432`，含 `makeUMPIRE`；隔离检出在同级 `ThirdParty-common` |
| Umpire | LLNL [v2025.03.0 官方归档](https://github.com/LLNL/Umpire/releases/tag/v2025.03.0)，源码已解包在隔离 ThirdParty 目录 |
| 编译器 | NVIDIA HPC SDK 24.9；已拉取 `nvcr.io/nvidia/nvhpc:24.9-devel-cuda_multi-ubuntu22.04`，构建容器为 `windcatcher-openfoam-gpu-build` |

源文件 `wmake/rules/General/Nvidia-gpu/c++` 在隔离检出中仅有一处本机改动：把默认 `-gpu=cc90,mem:unified,managed` 改为 `-gpu=cc86,mem:managed`。3090 不能沿用默认 cc90。WSL 下 `nvidia-smi -q` 的 Addressing Mode 为 `N/A`；这既不证明 HMM 可用，也不能代替运行时测试。

## 当前构建进展与风险

隔离卷 `windcatcher-openfoam-gpu-poc-20261111:/work` 中已有 CUDA 版 Umpire、GPU OpenFOAM 源码和编出的 `libOpenFOAM.so`、`simpleFoam` 与 `Test-std_execution`。首次执行 `src/Allwmake -j16` 时缺少 `FlexLexer.h`，已在构建容器安装 `libfl-dev` 并增量续编。主日志及退出码分别为 `/work/openfoam-src-build-j16.log` 和 `/work/openfoam-src-build-j16.exit`。目标应用编译脚本早先因 OpenFOAM bashrc 与 `set -u` 顺序问题退出；手动按正确顺序加载环境后，`simpleFoam` 和测试程序均已生成。

官方 `Test-std_execution` 在本机的 `-gpu=cc86,mem:managed` 模式下跑到并行插值时以 `cudaErrorIllegalAddress` 终止。一个独立的栈变量引用捕获探针复现了同样错误；改用 `mem:unified,managed` 编译虽成功，运行时明确报“此平台不支持 Unified Memory”。2026-11-12 的合成 `simpleFoam` 也复现非法显存访问，具体发生在读取 `p` 场、构造 `zeroGradientFvPatchField` 时。这个 GPU 分支在 RTX 3090 / 当前 WSL 环境未能完成 CFD。**不得添加** `org.windcatcher.gpu-offload=verified` 或 `org.windcatcher.gpu-cfd-smoke=passed` 镜像标签。

注意：`develop.openfoam.com/Development/ThirdParty-common` 的镜像提交 `1ed1f64` 落后且缺少 `makeUMPIRE`；需用上表 GitLab 仓库的 `1eb9432`，避免误判官方没有构建脚本。

## 构建复现与后续步骤

SDK 已拉取并检查。复现时不修改现有 CPU 容器或公网路由：

~~~powershell
docker pull nvcr.io/nvidia/nvhpc:24.9-devel-cuda_multi-ubuntu22.04
docker image inspect nvcr.io/nvidia/nvhpc:24.9-devel-cuda_multi-ubuntu22.04 --format '{{.Id}} {{.Size}}'
docker run --rm --gpus all nvcr.io/nvidia/nvhpc:24.9-devel-cuda_multi-ubuntu22.04 bash -lc 'nvc++ -V; nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader'
~~~

把隔离源码和 ThirdParty 挂载到容器的同级目录（例如 `/work/src`、`/work/ThirdParty-common`），先在容器里用 `ls` 验证挂载。不要从 E: 上不完整的 NTFS 检出构建；该仓库有 NTFS 不支持的冒号文件名。以下命令在已进入 SDK 容器且挂载成功后运行：

~~~bash
cd /work/src
source etc/bashrc WM_COMPILER=Nvidia-gpu
cd /work/ThirdParty-common
./makeUMPIRE -DENABLE_CUDA=ON -DUMPIRE_ENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DBLT_CXX_STD=c++17
cd /work/src
./Allwmake -j 8
~~~

上述 Umpire 参数来自官方 GPU [依赖说明](https://gitlab.com/api/v4/projects/62231285/wikis/dependencies)和 `makeUMPIRE` 的 `-DNAME=VALUE` 参数入口；具体编译仍需实测。OpenFOAM 的 [GPU 构建说明](https://gitlab.com/api/v4/projects/62231285/wikis/build)也要求启用 managed 内存池。构建或运行时检查日志里的 `memory pool : managed`；`not available` 或 `unused` 不是通过。

## 验收门禁

风见现有的 tests/test_bilateral_cfd_boundary.py:60 只通过 monkeypatch 验证 prepare_case 的边界配置；它没有生成真实体网格或运行求解器。可用它定位输入契约，并参考 tests/test_cfd_prep.py，但 GPU 门禁必须另做不依赖 monkeypatch 的合成病例全链路。

1. 用 CUPTI 对合成 `simpleFoam` 记录设备端 `CONCURRENT_KERNEL` 活动；`nvidia-smi` 看见 GPU 只能证明设备可见。实测 NVHPC 24.9 的 `-stdpar=gpu` 小程序在 `NVCOMPILER_ACC_NOTIFY=3` 下不输出 kernel 启动行，不能将该环境变量单独作为否定或肯定证据。本机 WSL 下 Nsight Systems 的 `cuda_api_sum` 能记录主机端 `cudaLaunchKernel` 调用，但 `cuda_gpu_kern_sum` 没有设备时间线；只有 API 调用不算通过。NVIDIA SDK 自带的 CUPTI 注入示例在同一合成程序上记录到设备端 `CONCURRENT_KERNEL`，可作为精简注入器的依据。验收还须同时核对程序正确退出和数值结果。
2. 在有效的合成 case 目录运行官方 `applications/test/std_execution/Test-std_execution`，确认 `std::execution::par_unseq` 不报 CUDA 错误。
3. 用**风见生成的合成病例 CFD 算例**运行 `simpleFoam`：保持生产生成的网格、边界类型和 `fvSolution.gpu`，包括 `twoStageGaussSeidel`；不能用普通教程算例代替此门禁。
4. 对同一合成算例跑 CPU 基线，比较收敛残差、流量守恒、压力降与现有数值质量门禁；记录峰值显存和耗时。求解器退出 0 不等于数值合格。
5. 仅在第 1–2 项通过后，才可给独立 GPU 镜像加 `org.windcatcher.gpu-offload=verified`、`org.windcatcher.gpu-arch=cc86`、`org.windcatcher.openfoam-flavour=esi`；第 3–4 项也通过后才加 `org.windcatcher.gpu-cfd-smoke=passed`。目标镜像名是 `windcatcher/openfoam-gpu:2606-cc86`，需与现有 `/openfoam/bash` 调用入口兼容。

当前真实 `simpleFoam` GPU 算例失败，CPU/GPU 数值对比未执行，GPU 选项应显示为未就绪，CPU 路径继续独立可用。

隔离测试补记：对官方 `Test-std_execution` 的插值 lambda 作按值捕获实验后，程序从有效的 OpenFOAM case 启动，180 秒内完成大数组顺序填充和并行填充（分别约 3.6 秒与 95.1 秒），但未跑完后续插值，退出码 124。这既不是官方测试通过，也不能据此断言 `simpleFoam` 的性能。实验源码已恢复为原版。

## 2026-11-12 合成求解结果

- 本地隔离候选镜像：`windcatcher/openfoam-gpu:2606-cc86-candidate`，image ID `sha256:e6f72bb12ac10ab0e597dcba39481cea8da117d341ef074710e64850a42686f4`。仅包含 NVIDIA 编译的运行时、CUPTI probe 和 wrapper；没有任何 GPU 验收标签，且没有用于公网部署。
- 候选镜像中的 `simpleFoam` 与 CUPTI probe 动态库依赖检查通过，`libcupti.so.12` 可解析；运行日志显示 `memory pool : managed`。
- 合成算例：`C:\Temp\windcatcher-gpu-acceptance-20261111\case-20261111-194536-44bb\cfd`，风见生成的双侧入口网格，约 226,699 个单元；通过只读 `fvSolution.gpu` overlay 运行。
- 原始日志：`C:\Temp\windcatcher-gpu-acceptance-20261111\case-20261111-194536-44bb\cfd\logs\gpu-simpleFoam-acceptance.log`；进程退出码：`139`。
- 失败位置：日志停在 `Reading field p`；异常为 `parallel_for: failed to synchronize: cudaErrorIllegalAddress`，栈包含 `zeroGradientFvPatchField<double>`。没有 SIMPLE 迭代、没有后处理采样，也没有 CUPTI 成功标记；`run_status.json` 未生成。
- CPU 参考目录中的基线虽有相同网格签名，但仅运行 100 次迭代且未收敛。此次 GPU 运行失败，不能计算加速比或声称数值一致；`compare_gpu_cfd.py` 没有运行。
- 修复范围：`Stage-RuntimeImage.ps1` 现在将 probe 打包到运行器约定的 `/openfoam/libwindcatcher_gpu_kernel_count.so`，并在 commit 前后检查运行时目录与 `libcupti.so.12`。本轮 GPU 契约与对比测试通过（30 passed）；这只验证软件门禁，不改变 GPU 求解失败结论。

故障调用链定位：`zeroGradientFvPatchField` 字典构造器调用 `extrapolateInternal()`，随后进入 `fvPatch::patchInternalField()`；其 `apply_policy` 在长度不少于 1000 时切换到 `par_unseq`，GPU lambda 以 `[&]` 捕获 host 侧 `UList` 包装对象。这个路径与 `Test-std_execution` 中引用捕获的 illegal-address 现象相符，是当前最强根因假设，尚未通过受控修改验证。下一步应在隔离构建副本中比较引用捕获与显式指针/view 捕获，并分别核实索引与底层数据的设备可达性；确认后重新通过 `Test-std_execution`，再运行合成 `simpleFoam`。在两项通过之前，不要做 CPU/GPU 数值比较、贴验证标签或启用用户界面选项。

## 2026-11-12 CUDA 12.6 隔离复测

- 在独立卷 windcatcher-openfoam-gpu-fvpatch-clean-20261112 中，以固定 CUDA 12.6 从零重编 finiteVolume，构建退出码 0。隔离镜像 windcatcher/openfoam-gpu:2606-cc86-fvpatch-clean-20261112（sha256:fb554ce99081f7d4658b9d6828040d2c6015fa1a7b45cffb206f3bb541262534）未加 GPU 验收标签。
- 用风见生成的双侧合成算例副本 C:\Temp\windcatcher-gpu-cleanbuild-smoke-20261112，设置 FOAM_MEMORY_POOL=managed; size=1024, incr=1024，单步 simpleFoam 依次读入 p、U，在创建 phi 时因 cudaErrorIllegalAddress 退出（134）；日志在 logs/simpleFoam.log。额外设置 CUDA_LAUNCH_BLOCKING=1 的独立副本也在 phi 失败（134），日志在 C:\Temp\windcatcher-gpu-cuda-blocking-20261112\logs\blocking-simpleFoam.log。由此不能再把故障仅归咎于 CUDA 11.8/12.6 混用。
- 将 surfaceInterpolationScheme.C 的 dotInterpolate 内层循环仅改为 std::execution::seq 的独立诊断版，编译退出码 0；合成单步测试越过 phi 并进入 Time = 1，但随后同样因非法显存访问退出（134），无 End、结果场或 CUPTI 成功标记。诊断镜像为 windcatcher/openfoam-gpu:2606-cc86-phi-seq-diagnostic-20261112，日志在 C:\Temp\windcatcher-gpu-phi-seq-smoke-20261112\logs\phi-seq-simpleFoam.log。串行化仅用于缩小首个故障范围，不是 GPU 修复。
- 当时计划在另一独立副本中保持 GPU 循环，将宿主侧 Field/UList 引用捕获改为底层指针按值捕获；geometricOneField 没有底层数组，需要保留按值对象分支。此对照已实施，结果见下节。完整官方测试、合成 CFD 收敛、数值门禁、CPU/GPU 对比仍未通过，工作台 GPU 选项继续禁用。
第二处崩溃定位补记：在另一全新合成副本 C:\Temp\windcatcher-gpu-time1-blocking-20261112 中，串行 phi 诊断镜像配合 CUDA_LAUNCH_BLOCKING=1 仍于 Time = 1 退出（134）。gdb 栈显示 simpleFoam UEqn.H 的 fvm::div(phi,U) → boundedConvectionScheme::fvmDiv → fvc::surfaceIntegrate 中的 GPU std::transform；日志为 logs\time1-gdb.log。该路径有宿主包装对象引用捕获和 cellReduceFunctor 的 labelUList 引用成员，属于待验证风险，不是已证实根因；需用另一隔离构建做单点对照。

## 2026-11-12 phi 指针捕获实验

- 隔离卷 `windcatcher-openfoam-gpu-phi-ptr-20261112` 中保留 GPU 执行策略，将 `dotInterpolate` 的场访问改为底层指针按值捕获，并对没有底层数组的 `oneField` 使用按值分支。`finiteVolume` 重编退出码 0（118 条重编命令）；独立镜像 `windcatcher/openfoam-gpu:2606-cc86-phi-ptr-20261112`（ID 前缀 `sha256:10c0035a2aa4`）没有 GPU 验收标签。
- 未改动的风见双侧合成算例副本 `C:\Temp\windcatcher-gpu-phi-ptr-smoke-20261112` 依次越过 `p`、`U`、`phi`，进入 `Time = 1`，随后 `cudaErrorIllegalAddress`，退出码 134；没有 `1/` 结果场、`End` 或设备内核成功标记。证据：`logs/phi-ptr-simpleFoam.log`、`logs/phi-ptr-simpleFoam.exit`。这支持首处故障与引用捕获有关，不证明 GPU CFD 已可用。
- 第二处故障仍待单点对照：`fvcSurfaceIntegrate.C` 的 GPU transform 引用捕获 `issf`，`cellReduceFunctor` 持有 `labelUList&`；`ListPolicy.H` 的 GPU 调度阈值 1000 与入 Umpire 池阈值 3000 不一致。普通 `new` 的设备可达性尚未实测，不能仅据阈值断定根因。工作台 GPU 选项继续禁用。

## 2026-11-12 Time=1 单点对照

- 独立卷 `windcatcher-openfoam-gpu-time1-seq-diagnostic-20261112` 保留早先的 phi 串行诊断，仅将 `fvcSurfaceIntegrate.C` 内部一个 GPU transform 改为串行；只重编 `simpleFoam` 成功（构建退出码 0）。此改动仅用于定位，不是可发布求解器。
- 原样双侧合成算例副本 `C:\Temp\windcatcher-gpu-time1-seq-smoke-20261112` 进入 `Time = 1`，随后在同一函数边界阶段的 `lduAddressing::calcPatchSort()` GPU `std::stable_sort` / Thrust merge sort 触发 SIGSEGV，退出码 139；没有 `1/` 数值场。日志：`logs/time1-seq-simpleFoam.log`。这表明单改内部归约不足以完成求解，排序路径还需单独审查。
- 另一独立卷 `windcatcher-openfoam-gpu-time1-ptr-20261112` 在已越过 phi 的指针版基础上，将 `fvcSurfaceIntegrate.C` 内部归约改为底层数组指针按值捕获。仅 targeted 重编 `simpleFoam` 成功（**非完整一致构建**，其余依赖尚未重编）；全新原样合成副本 `C:\Temp\windcatcher-gpu-time1-ptr-targeted-smoke-20261112` 越过内部归约，随后同样在 `calcPatchSort` GPU 排序 SIGSEGV，退出码 139，仍无结果场。日志：`logs/time1-ptr-targeted-simpleFoam.log`。这支持第二处引用捕获风险，但不能把单目标诊断版当成可部署版本。

## 2026-11-12 拓扑排序对照与当前门禁

- 独立卷 `windcatcher-openfoam-gpu-time1-sort-seq-diagnostic-20261112` 从 Time=1 串行归约诊断版克隆，仅将 `lduAddressing::calcPatchSort()` 的 GPU `stable_sort` 改为串行。定向重编 `libOpenFOAM` 成功。全新原样双侧合成算例 `C:\Temp\windcatcher-gpu-time1-sort-seq-smoke-20261112` 仍在 `Time = 1` 退出 134，无 `1/U` 等结果场；GDB 将下一处 GPU 异常定位到 `basicFvGeometryScheme::nonOrthDeltaCoeffs()` 的 `std::for_each`，调用链来自 `correctedSnGrad` 与 `gaussLaplacianScheme::fvmLaplacian`。日志：`logs/time1-sort-seq-simpleFoam.log`、`logs/time1-sort-seq-gdb.log`。
- 这说明当前 fork 在初始化、归约、拓扑排序和几何系数等不同路径连续出现设备端错误。代码审查还标出边界归约、梯度、smoother、GAMG 的待测风险，**不是已证实故障**。停止把单点串行化当作可发布修复；须先评估 fork 的系统性设备内存适配成本，再决定是否继续此 GPU 路线。现有 GPU 镜像均不加验收标签、不接入工作台；CPU 路径保持原状。通过门槛仍是官方测试、完整合成 CFD 收敛、数值质量、CPU/GPU 对照和设备内核证据。
