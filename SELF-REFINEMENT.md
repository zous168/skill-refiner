# skill-refiner 真实完善与完整自测（2026-10-18）

此文保留 v0.2 的历史自测记录；绝对路径链接指向原始本地工作区，完整运行档案未提交到 Git。v0.3 当前验证见 [验证记录](VERIFICATION.md) 和 [仓库内验证摘要](verification/README.md)。

**当前集成版本完整自测通过，程序状态为 `unchanged`。** 被测对象是完整现有 Skill（SKILL.md、引用资源和 Python 模块）；核心用例真实调用安装副本的 refiner，执行子 Skill 的自主规划、复审、支持脚本修复、回归和独立验收。

本轮外层自动修改轮数为 0。此前原生修复 Agent 根据两种模式的真实失败记录，自动生成了 agent.py 补丁；该补丁与人工工程修复整合后进入当前验证。若本轮为 unchanged，表示当前版本通过且无需新外层修改，不表示一次无人介入的自代码修复从头到尾成功。

## 实际结果

| 外层阶段 | Codex | 原生子 Agent |
|---|---:|---:|
| baseline | 2/2 | 2/2 |
| acceptance | 2/2 | 2/2 |

外层调用 5 次角色、执行 8 次用例，耗时 2557.67 秒。完整通过的内层流程 4 个，合计 32 次角色调用、28 次用例执行。每个通过的内层流程均核对实际 baseline FAIL（无 ERROR）、修复补丁、全量回归、新输入验收和已完成模型事件。角色数不等于供应商 HTTP 请求数。

baseline 行是集成版本的开发套件结果。若没有外层修复轮，就没有另一次外层候选回归；各内层业务流程确实执行了失败基线、代码修复、回归与独立验收。两个外层引擎使用完全相同的冻结开发集及验收集；内层测试由被测 refiner 每次自主规划。

## 修复来源与执行范围

- **自动生成的自身补丁**：原生修复 Agent 读取完整源码及实际被拒绝计划、审查意见，给 agent.py 补充管理执行器的来源／完整性检查上下文，解决把控制器约束误列为未覆盖业务行为的问题。[原始修复输出](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v2/run-1/iterations/round-1/changes.json) · [原生父子代理调用证据](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v2/run-1/agent-calls/008-repair/backend-result.json)。该次运行因外层审计错误中止，旧状态未改为成功。
- **人工工程完善**：逐用例审核安装源码约束、夹具、回答格式和命令可观察性；将未解决报告与规范错误分开，保留已证明的局部改进但继续阻止最终资格；回传有来源和SHA256的失败摘录；在清理前归档诊断。
- **实跑后的两项整合修正**：诊断 ZIP 改为 skill-up 接受的工作目录相对路径；自动补丁的管理执行器保证只用于确实实施该校验的后端配置。两种真实模式均确认 ZIP 导出与回读，集成版本再经历本轮完整验证。[整合记录](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/engineering-integration.json) · [实际传输验证](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v2/export-live-probe/verification.json)。

当前源码的30项兼容性测试、静态检查、skill-creator格式校验和可用性检查通过；独立规范复审也通过。兼容性测试包含确定性适配器和本地HTTP替身，不算真实模型证据。本轮矩阵和内层已完成CLI事件另外证明真实模型调用。

## 为什么复用固定流程审计器

模型生成的早期外层审计出现了实际错误：允许附加说明却只接受纯JSON、把Python子进程当成直接shell命令匹配、读取不存在的日志，以及比较整个安装／工作目录导致新增产物被误判为源码修改。已保留失败判定并中止这些无效测试，未改写冻结用例求通过。[第一轮记录](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018/run-1-cancellation.json) · [目录清单误判记录](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v2/invalid-workspace-manifest-oracle.json)。

本轮复用固定流程完整性审计器，模型仍自主生成子 Skill 代码、业务契约、独立输入／期望探针及开发／验收场景。被测 refiner 再独立规划业务测试并修复实现。固定审计器执行实际CLI，检查原文件、判据、候选、真实模型事件和补丁；不替模型生成业务答案或修复代码。其预检复用了先前真实通过的内层记录，并验证坏哈希被拒绝、共享目录允许新产物而禁止改旧文件。[审计器预检](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/auditor-preflight/verification.json)。

可复用实现：[流程审计器](/Volumes/work/skill-guide/tests/support/trusted_workflow_audit.py) · [独立工作流证据检查](/Volumes/work/skill-guide/tests/support/workflow_verifier.py)。它们与本轮固定副本字节相同。JSON文件CLI业务 fixture 的输入格式及执行方式见 [本轮测试规范](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/inputs/requirements.md)。

## 修改前后与原始证据

- [完整源码补丁](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/changes-full.patch) · [测试补丁](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/tests.patch) · [前后清单](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/before-after.json) · [原版](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/before/skill-refiner) · [当前快照](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/after/skill-refiner)。
- [程序状态](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/run-1/state.json) · [程序报告](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/run-1/report.md) · [独立证据核对](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/verification.json) · [原始角色调用](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/run-1/agent-calls)。
- [冻结开发计划](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/run-1/test-plan.json) · [冻结验收计划](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/run-1/acceptance-plan.json) · [判据锁](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/run-1/oracle.lock.json) · [harness与审计器哈希](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/harness.json)。
- [原ZIP与解包位置](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/evidence-location-map.json) · [文件SHA256索引](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/raw-index.json) · [30项测试原始输出](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/compatibility/unit.stderr)。原始绝对路径未改写，解包位置另行映射。

两个开发阶段诊断用例将详细记录写在归档范围之外的临时目录。独立核对后已从实际原目录回收14个原始文件，逐一验证唯一安装路径、固定审计器SHA256及进程记录，另外保存来源和文件哈希；未重建或回填旧日志，原工作区ZIP保持不变。补充的相同冻结用例复测两种模式均通过，并明确要求证据留在工作区。[原件回收映射](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/diagnostic-original-recovery.json) · [补充同例复测](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/diagnostic-retention-replay/verification.json)。

## 验证边界

本轮外层模式是Codex与实际原生子Agent，保留父子线程关系和完成事件；配置固定gpt-6.1-sol，内层角色使用任务配置的本机认证Codex。没有证明跨供应商一致性。Cursor和自定义模型端点仍未实测，本轮没有新增Claude实测。

这是有限真实任务的通过证据，不代表任意Skill都能稳定无人干预收敛。角色隔离不是操作系统读屏障；使用本机环境、私有TMPDIR和已记录的进程沙箱设置，无容器隔离。诊断归档及修复摘录有大小限制，遗漏会显式记录；本轮完整自测另外保留原始工作区ZIP；这不表示所有中断／超时错误路径都已验证能保留完整提供方事件。

2026-10-17未收敛记录仍保留，见 [原报告快照](/Volumes/work/skill-guide/.refiner-runs/self-audit-20261018-v3/before/SELF-REFINEMENT.md)。
