# GoalExecutor 字段与落地

完整快照以 `SKILL.md` 标准结构为准。本文件补目标追踪、回执契约、归档、软熔断计数和软件档规则。

默认是通用任务执行：把顶层目标拆成验收标准和关联待办；是否委派、是否能持久化产物取决于宿主。下文「软件工程档」只在用户明确要按 LT / 写代码仓库时启用，不要当成默认流程。

## 快照字段

| 字段 | 约束 |
|------|------|
| `task_id` | 首次按「根任务编号」生成后只读。格式 `GE-{actor}-{yyyyMMdd}-{HHmmss}-{rand4}` |
| `global_goal` | 顶层目标原文。主Agent不得改写 |
| `constraints` | 用户约束列表。可追加澄清项，不得删用户原句 |
| `success_criteria` | `[{ "id": "G01", "criterion": "可观察的完成条件", "status": "pending", "evidence_ref": null }]`。状态为 `pending` · `in_progress` · `verified` · `blocked` · `superseded` |
| `goal_changes` | 空数组或按时间追加的用户明确范围变更；保存原目标，不静默覆盖 |
| `todo_specs` | 所有待办的短规格：`subtask_id`、`title`、`goal_refs`、`done_when`；每条 pending 恰好对应一项 |
| `tasks` | 可选的 Viewer 层级投影：唯一非空 `id`、`parentId`、`title`、`status`；根节点 parentId 为空，其他父引用必须存在且无环。可执行叶节点 ID 对应 `todo_specs.subtask_id`，分组节点用独立 ID；状态为 `pending` · `in_progress` · `done` · `blocked`。主Agent同步状态；该树不替代 `todo_specs`、验收标准或证据。通用任务不写软件分层 |
| `current_subtask` | 当前 pending 第一条的短名，建议以对应 `subtask_id` + 中文冒号开头，供 Viewer 精确定位；无待办则为 `null` |
| `done` | `["A01：摘要完成 [G01] ref:art_001"]`。只许摘要、目标ID和引用 |
| `pending` | `["A03：待执行短名 [G01]"]`。只许短名和目标ID，不许正文 |
| `status` | `continue` · `complete` · `blocked`。pending 非空时禁止 `complete` |
| `block_reason` | `blocked` 时一句原因，否则 `null` |
| `artifacts_ref` | `{ "art_001": "一句话标题" }`。禁止贴全文 |
| `child_agent_calls` | 当前未完成调用的数组；默认最多一项。只在宿主支持并行且任务独立、范围不重叠时可多项 |
| `next_instruct` | 仅当 `continue` 且宿主支持自动续跑时给出；否则 `null` |

禁止把素材原文、脚本、分镜、代码 diff、测试日志写入快照任一字段。`art_ref` 只表示定位，不表示用户已收到交付物或验收已通过。

`pending` 用于快速查看进度；`todo_specs` 保存对应的可执行验收规格。派发前必须找到同 ID 规格并原样带入调用对象。待办完成并移出 `pending` 后，从 `todo_specs` 删除其规格；收包后不得临时改写 `done_when` 让结果看起来合格。

用户明确改变范围时，在 `goal_changes` 追加原话和时间；新增标准就追加新 ID，失效的旧标准保留并标为 `superseded`、注明变更来源。只有 `verified` 或经用户变更明确标为 `superseded` 的标准才算结清。


## 跨项目总览

项目清单是本机显式白名单，不扫描任意磁盘路径，也不应提交到仓库：

```json
{
  "version": 1,
  "projects": [
    { "id": "codex-project-id", "name": "project-name", "path": "D:/work/project-name" }
  ]
}
```

将清单保存为 Viewer 所在 `.tasks/project-sources.json`。`scripts/update_project_dashboard.py` 只检查每个登记目录的 `.tasks/snapshots/GE-*.json`，生成 `.tasks/project-dashboard.json`。汇总包含目标、状态、当前待办、已完成/待办数、验收进度、更新时间和快照内的可选子任务树，并附带供只读详情页使用的字段白名单快照投影及有限的 runtime 计数；不收集 prompt、日志或 artifacts 正文。项目总览只显示至少有一条 GoalExecutor 目标任务的项目；目标名称可点击打开对应任务详情，详情页通过「项目总览」返回。清单只表示已登记项目，不是自动发现或完整的操作系统目录清单。

生成命令（从宿主仓库根目录运行）：

```powershell
python "<large-task-tracking skill>/scripts/update_project_dashboard.py" --sources ".tasks/project-sources.json" --output ".tasks/project-dashboard.json"
```

每次新增登记项目或已追踪快照发生阶段更新后，主Agent重跑生成命令。Viewer 定时读取汇总文件；快照始终是进度和验收的唯一来源，详情页使用汇总文件中的白名单投影进行导航展示。

## 长流程子 Skill 进度交接

当一个领域 Skill 自己运行多阶段流程、测试或修复（例如 `skill-refiner`）时，GoalExecutor 主Agent仍是唯一的用户可见进度管理者；领域 Skill 保存可恢复的内部阶段和证据，不另建动态进度面板。单任务 Viewer 读取主Agent维护的快照/runtime；可选的项目总览读取由登记项目快照生成的 `project-dashboard.json`。两种视图都不接管调度或写入进度。

- **拆分父级待办。** 按阶段建立少量父级 `todo_specs`，例如「冻结并展示设计/测试计划」「执行有预算的流程」「验证最终交付」。每项有 `goal_refs` 和可检查的 `done_when`。子 Skill 的 `planned` 只表示计划就绪，不能算作测试或父级目标完成。
- **总快照只记摘要。** 父级快照的 `current_subtask`、`pending`、`done` 和标准状态表达用户能理解的阶段进展。把子 Skill 的工作目录、`state.json`、`stages.json` 和相关证据路径放入 runtime，或写入 `.tasks/artifacts/{task_id}/art_xxx.md` 的短索引文件，再在 `artifacts_ref` 只登记 art ID 与简短标题；不要把日志或测试结果全文复制进快照。
- **运行中更新。** 识别子 Skill 输出的结构化阶段事件。阶段切换或有结果时更新父级检查点；长测试中的心跳和当前用例/执行器摘要可写入 runtime，并用于简短用户进度说明。心跳不要求每次都增加 GoalExecutor 的 `round`；只有有意义的阶段、结果或阻塞变化才持久化主快照。
- **保留预算上限。** runtime 记录同一 run 开始时确定的时间、轮次、调用次数和评估次数上限，以及最近心跳。报告已用/剩余预算。执行器确实强制上限时按硬限制处理；否则这是主Agent必须遵守的停止条件，不是底层硬刹车。触顶后保留证据并停止；不能因进度暂缓、心跳刷新或重启新 run 而重置/放宽预算。
- **从同一 run 恢复。** 若中断，父快照保留原待办为 pending，runtime 保留同一工作目录和恢复命令/模式。先核对目标、冻结输入和子 Skill 的内部阶段清单，再用该 Skill 的原位恢复入口续跑。不可重新建目录、重复已经由子 Skill 验证完整的阶段，或仅因 `planned` 状态就从头运行。损坏/合同不一致时保留原证据并把该待办标为 blocked，写明需要的新 run 或用户决定。
- **展示设计与进度分离。** 子 Skill 的冻结计划（如设计、需求、验收标准、逐案测试清单）是计划产物；GoalExecutor 的快照/runtime 是执行进度。Viewer 只读并呈现主Agent维护的当前快照/runtime；不另写进度副本，也不要求子 Skill 再建 dashboard。

若宿主提供可交互的进程/session 工具，用可轮询的会话运行子流程，并将 session 标识与 run 目录写入 runtime；定期读取新增结构化 stdout 事件及 `state.json`/`stages.json`，在阶段切换、评估开始/结束、预算接近上限或阻塞时更新父级进度。不要启动无法观察或控制的 detached 流程。

子 Skill 没有持续输出或宿主无法监控时，不声称实时心跳；命令返回或阶段检查后读取内部状态文件，并据此更新父级进度。

## child_agent_calls

```json
{
  "subtask_id": "A03",
  "title": "写对照表",
  "role": "extract | create | implement | test | verify | review | business",
  "goal_refs": ["G01"],
  "done_when": "三家竞品的功能与价格字段都有可核验来源；未公开价格明确标为未知",
  "reasoning_effort": "仅在宿主支持时填写",
  "file_scope": [".tasks/artifacts/{task_id}/art_003.md"],
  "prompt": "发给子Agent的单一任务指令",
  "load_refs": ["art_001"],
  "material_refs": []
}
```

`child_agent_calls` 是主Agent维护的上述调用对象数组。每条 pending 必须对应 `todo_specs` 中一条非空 `goal_refs` 和具体 `done_when`；派发调用必须复制该规格。调用对象中的文件范围用于判定是否安全并行；无并行能力时保持数组最多一项。并行依赖宿主明确提供该能力，不代表上下文隔离。子Agent不得自行再派发任务，也不得写共享快照、runtime、current 指针、任务索引或 Viewer 数据；超出范围时向主Agent返回缺口或 `blocked`。

| 字段 | 用途 |
|------|------|
| `role` | 决定任务包口吻与回报头 |
| `goal_refs` | 本任务直接服务的验收标准 ID；不得为空 |
| `done_when` | 可观察的完成条件，必须能由回执或证据判定 |
| `reasoning_effort` | 宿主支持单独配置时才填；使用该宿主承认的值 |
| `file_scope` | 涉及仓库写入时列明可改路径；判定并行冲突的依据 |
| `prompt` | 只含本条任务、目标标准、范围、约束和需要的 ref；不要塞全部历史对话 |
| `load_refs` | 需要历史材料时提供引用；执行者按权限读取。主Agent也可为验收读取最小必要内容 |
| `material_refs` | 用户点名的外部素材编号。能给路径就给路径，不给正文 |

`role=verify` 用于独立复核。主Agent可读取最终用户交付所需的最小内容；禁止仅为控制上下文而跳过验收。

## 子Agent回执

子Agent结束时只准回这段 JSON（可包在【回执】代码块里）：

```json
{
  "subtask_id": "A03",
  "status": "done | blocked",
  "goal_refs": ["G01"],
  "evidence_refs": ["公开产品页 URL", "art_003"],
  "delivery_mode": "artifact | inline",
  "art_ref": "art_003 or null",
  "summary": "一句话摘要，不超过80字",
  "gaps": [],
  "block_reason": null
}
```

`status=done` 时 `goal_refs`、`evidence_refs`、`delivery_mode` 必填，并报告未解决缺口（无缺口用空数组）；`delivery_mode=artifact` 时 `art_ref` 必填，`inline` 时须把实际交付内容一并返回，不能只给摘要。只有主Agent能核验 `done_when` 且可取得交付时才可标 `done`。`blocked` 时 `art_ref` 可为 `null`，`block_reason` 必填，并保留已取得的证据和缺口。

主Agent收包后：

1. 按 `subtask_id` 找到对应待办，核对回执的 `goal_refs`、`evidence_refs`、`gaps` 与 `done_when`；证据不足时不得移入 done。
2. 证据充分时将该待办移入 done，例如 `A03：<summary> [G01] ref:art_003`，从 `pending` 与 `todo_specs` 移除，并更新对应标准的状态和证据引用。
3. 在 `child_agent_calls` 数组中只移除已收包的该 `subtask_id`；保留仍在运行的其他调用。
4. 仍有待办或必需标准未验证 → `status=continue`；只有全部标准验证且最终交付可访问时才设 `complete`。
5. 有持久化能力时写 `.tasks/snapshots/{task_id}.json` 与 `.tasks/runtime/{task_id}.json`，并更新 `.tasks/current-{actor}.json`；不支持时用宿主可用的检查点格式。

## 根任务编号

多人共用一个仓库时，根编号冲突来自公共 `snapshot.json` 和纯时间戳 `GE-YYYYMMDD-HHMMSS`。按下面发号。`LT-*` 与 `nextTopLevelSeq` 只出现在软件档，默认不用。

### actor

按顺序取第一个非空，再规范化：

1. `git config user.email` 的 `@` 前一段
2. `git config user.name`
3. 环境变量 `USERNAME` 或 `USER`
4. `anon`

规范化：转小写，去掉所有非 `[a-z0-9]` 字符，截断 16 位。空则 `anon`。同机同 git 身份视为同一 actor。

### GoalExecutor `task_id`

```text
GE-{actor}-{yyyyMMdd}-{HHmmss}-{rand4}
```

例：`GE-ling-20261027-110831-a3f2`

- `{yyyyMMdd}-{HHmmss}` 用本地时间，24 小时制
- `{rand4}` 4 位小写 `[0-9a-f]`，不要用 `0000`
- 落盘前检查 `.tasks/snapshots/{task_id}.json`、`.tasks/runtime/{task_id}.json`、`.tasks/artifacts/{task_id}/`（以及旧路径 `.tasks/{task_id}.json`）是否已存在；存在则重抽 `rand4`，最多 5 次
- 生成成功后只读，禁止改写或复用
- 禁止再发 `GE-YYYYMMDD-HHMMSS` 这种无 actor、无随机段的号

旧编号 `GE-20261027-110400` 只读兼容。新建必须带 actor。软件档的 `LT-{actor}-{NNN}` 见文末「软件工程档」，用户没点名则不创建。

### 续做与接力

- 本 actor 续做：读 `.tasks/current-{actor}.json` 的 `task_id`，再读对应快照
- 本 actor 有多份 `status=continue` 快照、且 current 缺失：列出 `task_id` + `global_goal` 一句，问用户跟哪条，不要猜
- 两人接力同一条根任务：沿用对方已有 `task_id`，不要新开号；`current-{actor}.json` 可以指向同一 `task_id`
- 各开各的任务：各发各的号，文件互不覆盖

## 归档

目录（目标 Git 仓库根，不是模板目录）：

```text
.tasks/
  current-{actor}.json
  snapshots/{task_id}.json
  runtime/{task_id}.json
  artifacts/{task_id}/
    art_001.md
  index.json                 # 仅软件档
  LT-{actor}-001.json        # 仅软件档
```

禁止写公共 `.tasks/snapshot.json`、`.tasks/runtime.json`、`.tasks/current.json`。若仓库里还有旧的这三份，先核对目标文件是否已存在；保留两边原件并合并信息，不得覆盖或删除。验证迁移后的快照和指针可读、目标 ID 一致后，再停止使用旧路径。

`art_xxx` 只在该 `task_id` 目录内三位递增，不复用。跨 `task_id` 允许都从 `art_001` 起。归档文件头部写：

```text
---
id: art_001
task_id: GE-ling-20261027-110831-a3f2
subtask_id: A01
title: 竞品与信息源
---
```

其后才是正文。按需读取有权限的归档；验收或交付需要时，主Agent可查看最小必要内容，不得因控制上下文而跳过核验。`art_ref` 只是定位信息，不能代替用户可访问的交付物。

## runtime（软熔断计数）

路径：`.tasks/runtime/{task_id}.json`。

```json
{
  "round": 1,
  "max_rounds": 20,
  "last_fingerprint": null,
  "stagnant_streak": 0
}
```

- 每次完成并持久化一个有意义检查点时 `round += 1`：初始化拆分、收到结果、处理用户澄清或恢复；仅等待运行中的子Agent不计数
- `round >= max_rounds` 且仍有未完成标准/待办 → `status=blocked`，`block_reason` 写预算上限已到；在开始下一项工作前执行此判断。若 `round > max_rounds`，视为预算状态越界，同样停止并报告。全部必需标准已验证时可按完成门禁设为 `complete`。
- 首个检查点记录 `last_fingerprint` 并将 `stagnant_streak` 设为 0。之后的指纹至少包含 `success_criteria` 状态及证据引用、`goal_changes`、`todo_specs`、`done`、`pending`、未完成调用 ID 和全局状态；不要纳入时间戳、轮数等每次都会变化的字段。与上次检查点相比没有新证据、标准变化或待办进度时，`stagnant_streak += 1`；有进展则清零
- `stagnant_streak >= 2` → `status=blocked`，`block_reason` 写连续两个已完成检查点没有新证据或进展
- 用户提供解除阻塞所需信息后，沿用原 `task_id`，恢复阻塞待办并重新验证，不得重写原目标

这是提示词约定，不是底层硬刹车；只有子 Skill 的执行器确实强制其 run 预算时，该项预算才是程序级限制。GoalExecutor 必须沿用同一 runtime/run 指针计数，不得通过新建目录或重新启动 run 清零。

## 子Agent prompt 模板

```text
你是当前宿主提供的执行者；上下文是否隔离由宿主决定，不得假设或承诺隔离。
主Agent是唯一调度者和共享进度状态写入者。本轮只执行这一件事，做完即停；不得创建、调用或委派给其他Agent，不得修改共享快照、runtime、current 指针、任务索引或 Viewer 数据。

【subtask_id】A03
【title】写对照表
【role】create
【goal_refs】G01
【done_when】三家竞品的功能与价格字段都有可核验来源；未公开价格明确标为未知
【file_scope】.tasks/artifacts/{task_id}/art_003.md
【reasoning_effort】仅在宿主支持时填写
【constraints】只依据公开信息；禁止编造报价
【material_refs】（仅提供完成本任务所需的素材路径或引用）
【load_refs】art_001 → .tasks/artifacts/{task_id}/art_001.md（仅在需要时读取）
【归档】宿主支持持久化时写入 .tasks/artifacts/{task_id}/art_003.md，头部带 id/task_id/subtask_id/title；否则按宿主方式交付
【回执】返回 JSON：subtask_id、status、goal_refs、evidence_refs、delivery_mode、art_ref（inline 时可为 null）、summary（≤80字）、gaps、block_reason；inline 时附实际交付内容
【禁止】扩展范围、改写 global_goal、跳过证据或 done_when、执行下一件 pending；回传正文按宿主上下文成本和主Agent验收需要控制
```

仅软件档才追加【任务包】段（见下）。

---

# 软件工程档（可选）

仅当用户明确说按 LT、写代码仓库、或当前 `.tasks/LT-*.json` 还有未完成 L3 时启用。其他任务只用快照，不建 Epic、不跑 Gate。

启用后：GoalExecutor 调度 + `.tasks/` 任务库。

操作入口若仓库带 `.rules/templates/large-task-tracking/`，只复制其中软件 Epic 所需的 schema 与脚本到目标仓库 `.tasks/`。Viewer 由通用 GoalExecutor 初始化提供；若项目已有专用 Viewer，先核对其读取的快照 schema 并保留，不覆盖。禁止在模板目录里改文件、跑 `new-epic.ps1`、或把示例 Epic 留在正式库里。字段与门禁若与 `.rules/ai-dev-workflow.md`（「多 Agent 调度模式」「大型任务跟踪」「切片完成定义」）冲突，以规则正文为准。

## 初始化

目标是**当前改动的那个 Git 仓库**根目录下的 `.tasks/`。

```powershell
$src = "<workspace>/.rules/templates/large-task-tracking"
$dst = "<repo>/.tasks"
# Before creating anything, inspect the destination. If it exists and has content, preserve it.
if (Test-Path -LiteralPath $dst) {
  if (Test-Path -LiteralPath $dst -PathType Leaf) { throw "目标路径是文件；保留现状并停止初始化。" }
  $existing = Get-ChildItem -LiteralPath $dst -Force
  if ($existing.Count -gt 0) { throw "目标目录已有内容；请逐项核对并仅补缺，禁止覆盖。" }
}
New-Item -ItemType Directory -Path "$dst/snapshots","$dst/runtime","$dst/artifacts" -Force | Out-Null
Copy-Item "$src/schema-index.json","$src/schema-epic.json","$src/new-epic.ps1","$src/verify_preview.ps1","$src/preview.ps1","$src/preview.cmd" $dst -NoClobber
if (Test-Path -LiteralPath "$dst/index.json") { throw "index.json 已存在；保留原文件并停止初始化。" }
@'
{
  "libraryVersion": 1,
  "project": "<repo-name>",
  "updated": "<ISO-8601 +08:00>",
  "nextTopLevelSeq": 1,
  "epics": []
}
'@ | Set-Content -Encoding utf8 "$dst/index.json"
powershell -NoProfile -ExecutionPolicy Bypass -File "$dst/new-epic.ps1" -Title "Epic：<中文标题>" -Project "<repo-name>"
```

`new-epic.ps1` 只在 `.tasks/` 里执行。脚本若仍按 `nextTopLevelSeq` 产出 `LT-NNN`，生成后立刻改成 `LT-{actor}-{NNN}` 并重扫 `index.json`。编号以磁盘扫描为准，只增不复用。

预览：在 `.tasks/` 启动可观察的本地会话 `python -m http.server 8765 --bind 127.0.0.1`，再按 [SKILL.md](SKILL.md) 打开 Viewer。只绑定回环地址；若专用 Viewer 有不同 URL 参数，以其已核对的说明为准。模板目录的 8766 只用于模板自检。

无模板仓库时，至少建 `snapshots/`、`runtime/`、`artifacts/`，以及本 actor 的 `current-{actor}.json`。

## 三层

| 层 | tier | 编号 | 谁执行 | layers |
|----|------|------|--------|--------|
| L1 | epic | `LT-{actor}-001` | 不执行 | 六层全 `na`，进度看子节点 |
| L2 | phase | `LT-{actor}-001.01` | 不执行；模块收尾做一次点验 | 除 `ui` 外五层 `na`；有用户路径时维护 `businessFlows` 和 `layers.ui` |
| L3 | slice | `LT-{actor}-001.01.01` | **本轮 Implement 只对准这一层** | `backend` / `frontend` / `integration` / `unitTests` / `e2e` 要填；`ui` 一律 `na` |

Plan 必须同时有至少 1 个 L2，且每个 L2 至少 1 个 L3。分解结果先输出【分解审核】，用户确认后才写代码。用户当次已经说「按此分解开工」或「从 LT-xxx 开始」时不必再问。

L1 登记 `roleMatrix`。有用户路径的 L2 至少一条主路径 `businessFlows`。

pending 映射：`A01：LT-ling-001.01.01 开发 · 标题`、`A02：LT-ling-001.01.01 测试 · 标题`。同一 L3 必须串行：开发 → 测试 → Gate。测试未 PASS 不得开下一 L3。默认按队列执行；只有宿主明确支持并行，且不同 L3 之间没有文件重叠或依赖时才可并行。并行时在 `child_agent_calls` 中逐项登记 `subtask_id`、`goal_refs`、`done_when` 和 `file_scope`，由主Agent按 ID 收包并串行写 JSON；禁止两个子Agent改同一文件。宿主不支持并行时不得同时派发。

## 任务包

子Agent `role` 为 implement / test / business / review 时，`prompt` 追加：

```text
【角色】开发 | 测试 | 业务 | 审核
【L3】LT-xxx.xx.xx · 标题
【docAnchor】
【scopeIn / scopeOut】
【本轮 layers 目标】只列本轮要推进的层
【命令/入口】testCommands · url · 日志路径
【businessFlows】流 ID 与本轮步骤 order（测试/业务必填）
【禁止】扩 scope · 不跑测标 done · 子 Agent 跳过回报 · 把 diff/日志回传主Agent
```

| 角色 | 归档写什么 | 回执 summary 必含 |
|------|------------|-------------------|
| 开发 | 【开发交付】diff 摘要、路径、自测命令与结果、是否要补测、阻塞 | 改了哪些路径、自测过没 |
| 测试 | 【测试交付】命令、PASS/FAIL、log、flows 步骤 status、testPoints | 命令与 PASS/FAIL |
| 业务 | 【业务交付】文档路径、范围变更、待确认项 | 文档路径 |
| 审核 | 【审核结论】只读，四段结构见规则「审核任务」 | 通过/不通过一句 |

完整交付进 `art_xxx`，不进快照。

## Gate

未通过不得把对应 pending 标 done，不得收工。Gate 本身是主Agent的轻量自检，或单独派发 `role=verify` 子Agent；核对目标映射、回执与证据字段，并按需查看最小必要交付内容。不得仅凭回执或文件存在就判定内容正确，也不得因材料较长而跳过验收。

**A 开工前**：L3 ID 与 taskCard 一致；分解已确认；docAnchor 已读（由子Agent读）；roleMatrix 已登记或注明无多角色；有用户路径的 L2 已有主路径流；前序切片已 done。

**B 开发收包后**：交付在 scope 内；自测已跑或标明须补测；难点已写成【难点阻塞】，没有用兜底绕过去。

**C 测试收包后**：unit/e2e 有证据，FAIL 已分流或重测全绿；testPoints 已回写；本轮 businessFlows.steps 不再全是 pending；主路径未 verified 时 L3 不得 done；本轮目标层有据。

**D 收工前必须输出（可与快照同轮，仍禁止贴长文本）：**

```text
【调度 Gate】A/B/C · 通过 / 未通过（未通过项）
【任务库】index + LT-*.json · updatedAt · 是/否
【当前 L3 状态】status · 各 layer · 下一动作（一句）
【子 Agent 汇总】开发/测试/业务/审核 · 通过/阻塞/跳过 · art_ref
【调度决策】续做本 L3 · 下一 L3 · 模块点验 · 总验收 · 暂停（原因）
```

会话结束只能是这四态之一：L3 `in_progress` 且写明下轮从哪一层续；L3 `done` 且硬门禁满足；`blocked` 并写明难点或待确认；Epic `acceptance.passed`。禁止聊到一半不更新 JSON / 快照就停。

## 完成门禁

| 节点 | done 条件 |
|------|-----------|
| L3 | `backend` `frontend` `integration` `unitTests` `e2e` 均为 `done` 或任务卡声明的 `na`；`ui` 为 `na`；`integration` 为 `done` + `mode: real`；任一层 `mock` 不得 done；testPoints 已迭代并有证据 |
| L2 | 其下 L3 均 `done` 或 `cancelled`；有 UI 时 `layers.ui` checklist 全绿且覆盖 interaction / responsive / style；主路径 `businessFlows` 为 `verified` |
| L1 | 全部 L3 完成且 `acceptance.passed` |

每个节点 done 就同步 JSON（`status`、`layers`、`changelog`、库级 `updated`、`index.json` 摘要）和 GoalExecutor 快照。批量连做也不许攒到最后再记账。

【模块点验】在该 L2 下最后一片 L3 done 之后、开下一模块之前输出：模块 ID、入口 url、checklist 通过数、断点与样式结果、L2 是否已更新。点验可派 `role=verify` 子Agent，主Agent只收摘要。

## 续做

先确认当前请求明确指向该任务，再读 `.tasks/current-{actor}.json`、对应快照和 runtime。比较当前请求与 `global_goal`、`success_criteria`、`goal_changes`；无关请求照常处理且不改快照。输出【任务恢复检查】：`task_id`、当前目标、`current_subtask`、pending 第一条、未验证标准和 `round`。软件档再读 `index.json` 和当前 Epic：当前 L3 ID、未完成层、上一片是否已在 JSON 里 `done`。上一片未 done 时禁止换切片，除非用户当轮明确改了优先级或范围并记录到 `goal_changes`。若阻塞所需信息已由用户提供，沿用原 `task_id` 恢复对应待办并重新核验。current 指针是线索，不是恢复授权。

按需读取有权限的 `artifacts/` 内容；验收和用户交付需要时，查看最小必要部分。ref 不能代替实际交付物或验收证据。

## 任务库字段

完整 Schema：目标仓库 `.tasks/schema-epic.json`（从 `.rules/templates/large-task-tracking/schema-epic.json` 复制）。正文解释在 `.rules/ai-dev-workflow.md`「大型任务跟踪」。

### index.json

`libraryVersion`、`project`、`updated`、`nextTopLevelSeq`、`epics[]`。每条 epic：`id`、`file`、`title`、`status`、`updated`。`nextTopLevelSeq` 只作兼容，发号以磁盘扫描 `LT-{actor}-NNN` 为准；写回前重扫根 `LT-*.json` 重建 `epics[]`。

### L1 roleMatrix[]

| 字段 | 含义 |
|------|------|
| `role` | 角色标识 |
| `label` | 中文简称 |
| `docAnchor` | 活文档锚点 |
| `entryPoints[]` | 该角色入口 |
| `notes` | 能力边界一句 |

### L2 businessFlows[]

| 字段 | 含义 |
|------|------|
| `id` | 稳定 ID，如 `BF-knowledge-upload` |
| `role` | 对应 `roleMatrix.role` |
| `module` | 业务模块 |
| `pages[]` | `{ channel, path }` |
| `summary` | 主路径一句 |
| `docAnchor` | 需求/接口锚点 |
| `codeRefs[]` | 实现路径 |
| `status` | `draft` · `ready` · `in_progress` · `verified` · `blocked` |
| `steps[]` | 见下表 |

`steps[]`：`order`、`kind`（`business` · `setup` · `action` · `assert` · `cleanup`）、`action`、`expected`、`testKind`（`unit` · `e2e` · `integration` · `ui` · `manual` · `api`）、`command`、`codeRef`、`status`（`pending` · `pass` · `fail` · `skip` · `blocked`）、`verifiedAt`、`verifiedBy`、`note`。

### layers

每层：`status`（`pending` · `in_progress` · `done` · `blocked` · `na`）+ `mode`（`none` · `mock` · `real`）+ `note`。可选 `evidence`：`command`、`result`（`PASS` · `FAIL` · `SKIP`）、`at`。

六层名：`backend`、`frontend`、`integration`、`unitTests`、`e2e`、`ui`。

`integration` 的 mode 只用 `none` 或 `real`。`e2e` 的 mode 只用 `none` 或 `real`。

### L2 layers.ui

`url`、`viewports[]`、`styleAnchor`、`checklist[]`。

未写死断点时用 `["375", "768", "1280"]`。

checklist：`order`、`category`（`interaction` · `responsive` · `style`，缺省 `interaction`）、`viewport`（responsive/style 必填，或 `all`）、`operation`、`expected`、`status`（`pending` · `pass` · `fail` · `skip`）。

`ui.status: done` 要求 `mode: real`，每步 `pass` 或带 note 的 `skip`，三类 category 都有覆盖。

### L3 taskCard.testPoints[]

`order`、`kind`（另含 `responsive` · `style` · `business`）、`scenario`、`expected`、`command`、`status`（另含 `na`）、`flowRef`、`flowStepOrder`、`addedFrom`、`note`。

UI 点选只记在父模块 L2 的 `layers.ui.checklist`，不记在 L3。
