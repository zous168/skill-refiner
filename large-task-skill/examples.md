# GoalExecutor 调用样例

示例都显式映射「用户目标 → 验收标准 → 待办 → 证据」。委派、持久化、自动续跑和并行都依赖宿主实际支持；不支持时由主Agent按相同契约执行或说明限制。

有本地文件与浏览器能力时，初始化快照后打开只读 `viewer.html?task_id=<task_id>`。Viewer 读取 `.tasks/snapshots/` 与 `.tasks/runtime/` 并定时刷新；只有主Agent更新这些共享进度文件。

## 1. 通用：竞品调研（默认用法）

> 使用GoalExecutor，顶层目标：调研三家竞品的定价与功能差异，整理对照表，并写一份给销售用的一页纸话术；约束：只依据公开信息，禁止编造报价

### 初始化轮（只拆分）

```json
{
  "task_id": "GE-ling-20261027-110831-a3f2",
  "global_goal": "调研三家竞品的定价与功能差异，整理对照表，并写一份给销售用的一页纸话术",
  "constraints": ["只依据公开信息", "禁止编造报价"],
  "success_criteria": [
    {"id": "G01", "criterion": "三家竞品的功能和定价均有公开来源；未公开价格标为未知", "status": "pending", "evidence_ref": null},
    {"id": "G02", "criterion": "一页纸话术可供销售使用，且每项事实符合约束", "status": "pending", "evidence_ref": null}
  ],
  "goal_changes": [],
  "todo_specs": [
    {"subtask_id": "A01", "title": "圈定三家竞品与信息源", "goal_refs": ["G01"], "done_when": "确认三家竞品并列出可核验的公开来源；未知价格明确标注"},
    {"subtask_id": "A02", "title": "整理定价与功能对照表", "goal_refs": ["G01"], "done_when": "三家竞品功能和定价逐项有来源或明确标为未知"},
    {"subtask_id": "A03", "title": "写销售一页纸话术", "goal_refs": ["G02"], "done_when": "交付一页销售话术，所有事实符合约束"},
    {"subtask_id": "A04", "title": "核对事实、约束和交付", "goal_refs": ["G01", "G02"], "done_when": "核对来源与宣传约束，并确认最终文件可访问"}
  ],
  "current_subtask": "A01：圈定竞品与信息源",
  "done": [],
  "pending": [
    "A01：圈定三家竞品与信息源 [G01]",
    "A02：整理定价与功能对照表 [G01]",
    "A03：写销售一页纸话术 [G02]",
    "A04：核对事实、约束和交付 [G01,G02]"
  ],
  "status": "continue",
  "block_reason": null,
  "artifacts_ref": {},
  "child_agent_calls": [],
  "next_instruct": null
}
```

### 派发 A01

只有宿主支持调用子Agent且用户未要求单Agent时才派发；否则主Agent自己执行该项。

```json
{
  "subtask_id": "A01",
  "title": "圈定三家竞品与信息源",
  "role": "extract",
  "goal_refs": ["G01"],
  "done_when": "确认三家竞品，并为功能和定价信息列出可核验的公开来源；未公开价格标为未知",
  "file_scope": [".tasks/artifacts/GE-ling-20261027-110831-a3f2/art_001.md"],
  "prompt": "只圈定三家竞品和可核验的公开信息源。禁止编造报价。不得再委派任务或写入共享进度状态。按宿主支持的方式交付，并按回执契约报告证据与缺口。",
  "load_refs": [],
  "material_refs": []
}
```

子Agent回执示例：

```json
{
  "subtask_id": "A01",
  "status": "done",
  "goal_refs": ["G01"],
  "evidence_refs": ["三家竞品官网链接", "art_001"],
  "delivery_mode": "artifact",
  "art_ref": "art_001",
  "summary": "已确认3家竞品及公开信息源",
  "gaps": [],
  "block_reason": null
}
```

主Agent按 `subtask_id` 匹配回执，检查 `done_when` 和证据；证据不足就保留待办或补充核验。后续 A02、A03 需要读取 `art_001` 时再将其加入 `load_refs`。A04 核对两项标准并确保最终交付可访问。只有 G01、G02 都有充分证据、无未解决缺口且用户能取得对照表和话术时，才能将标准标为 `verified` 并结束任务。

## 2. 内容：门店短视频

> 使用GoalExecutor，顶层目标：基于门店素材，提取卖点、写短视频脚本、生成9秒写实分镜、生成配音提示词；约束：本地生活风格，禁止虚假宣传，素材引用@shop_003

可设验收标准：`G01` 卖点均能由 `@shop_003` 支持；`G02` 脚本、分镜和配音提示词完整且符合本地生活风格；`G03` 所有宣传内容无虚假或素材未支持的主张。pending 示例：`A01：提取卖点 [G01]` → `A02：写短视频脚本 [G01,G02,G03]` → `A03：生成9秒写实分镜 [G02,G03]` → `A04：生成配音提示词 [G02,G03]` → `A05：核对素材依据和交付 [G01,G02,G03]`。每项待办都应填写自己的 `done_when`，并在收件时按证据逐项核对。

## 3. 软件交付（可选档，用户点名才用）

用户：按 LT 做完知识库上传模块，调度多 agent。

这时才建 `LT-{actor}-001.json`，pending 写成：

```text
A01：LT-ling-001.01.01 开发 · 上传接口与校验 [G01]
A02：LT-ling-001.01.01 测试 · 上传单测与 e2e [G01]
A03：LT-ling-001.01.02 开发 · 上传页与进度 [G02]
A04：LT-ling-001.01.02 测试 · 上传页 e2e [G02]
A05：LT-ling-001.01 模块点验 [G01,G02]
```

每个待办还要列出具体 `done_when`、文件范围和证据。`role=implement` 时 prompt 带【任务包】。同一 L3 按开发 → 测试 → Gate 串行；不同 L3 只有宿主支持并行且文件范围不重叠、无前置依赖时才可并行。完整交付按宿主能力保存，回执报告目标标准、证据引用、摘要和缺口。主Agent按 Gate 验收并确认交付可访问后，才可将对应标准标为 `verified`。
