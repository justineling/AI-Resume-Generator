# 简历生成管线 / Pipeline

> **这篇讲什么**：`pipeline.run_chat()` 的完整阶段与进度、三层提示词契约、覆盖清单的生成规则、「禁止编造可证伪事实」如何落地、取消机制、以及无 Key 时的 mock 降级链路。
> **适合谁读**：要改生成逻辑、调提示词、或排查「为什么简历里出现了资料里没有的内容」的开发者。
>
> 权威来源：`backend/app/pipeline.py`、`backend/app/prompts.py`、`backend/app/mock.py`、`backend/app/llm.py`。

---

## 1. 阶段总览（真实顺序与进度）

`run_chat(chat_id, should_cancel=None) -> resume_id`。**每个阶段开始前检查一次取消**，每步写 `progress` + `stage` 并广播 `chat_progress`。

| 进度 | `stage` 常量 | 文案 | 做了什么 |
|---|---|---|---|
| **10%** | `STAGE_LOAD` | 载入资料快照 | 读 `Chat.raw_text`（超 `MAX_RAW_TEXT=20000` 截断）、`_profile_from_snapshot` 归一化、空资料则抛错、`build_client(session)` |
| **25%** | `STAGE_ANALYZE` | 解析岗位要求 | LLM `chat_json(SYSTEM_ANALYZE, build_analyze_user)` 或 `mock.analyze_job` → `_normalize_analysis`；截断时 `analysis["truncated"]=True`；回填 `chat.job_analysis`，必要时用 `job_title` 更新标题 |
| **60%** | `STAGE_GENERATE` | 规划版式并生成内容 | LLM `chat_json(SYSTEM_GENERATE, build_generate_user)` 或 `mock.generate_spec` → `_normalize_spec`；pop 出 `coverage` / `coverage_summary` |
| **85%** | `STAGE_RENDER` | 渲染简历版式 | `render_resume_html(normalized)` —— 纯确定性，无 LLM |
| **100%** | `STAGE_DONE` | 完成 | 落库（覆盖语义）+ 写完成 `ChatMessage(notice)` + 广播 `chat_progress` 与 `chat_updated` |

### 落库阶段（100% 内部）

`run_chat` 在同一个事务里：

1. 计算 `resume_title`（优先级：`chat.title` → `analysis.job_title` → `header.headline` → `"简历"`）；
2. 查出该 chat **已有全部 Resume**，记录 `old_html` 与 `prev_version`，**删除旧 EditLog 后删旧 Resume**（覆盖语义）；
3. 新建 `Resume(chat_id, title, theme, html, spec=normalized, coverage=coverage_obj, version=prev_version+1 或 1)`；
4. 若有 `old_html` → 写一条 `EditLog(instruction="重新生成", scope="whole", before_html=old, after_html=new)`；
5. 更新 `chat.status="succeeded"`、`progress=100`、`stage=STAGE_DONE`、`finished_at`；
6. 追加 `ChatMessage(role="assistant", kind="notice", content="已生成简历：{title}", meta={"resume_id":…})`。

---

## 2. 提示词分层（`prompts.py`）

| 常量 | 用途 | 输出契约 |
|---|---|---|
| `SPEC_CONTRACT` | 拼进 `SYSTEM_GENERATE` 的 spec 结构说明 | theme/layout/header/sections/match_notes/coverage 的字段与各 `type` 的 `content` 结构 |
| `COVERAGE_CONTRACT` | 拼进 `SYSTEM_GENERATE` 的覆盖清单说明 | `coverage[]` 元素结构 + `summary` |
| `SYSTEM_ANALYZE` | 岗位解析 | `sections` / `requirements`（带稳定 id）/ `employer_questions` / `tech_stack` / `domain_keywords` / `must_have` … |
| `SYSTEM_GENERATE` | 版式规划 + 内容生成 + 覆盖清单 | resume spec JSON（含 coverage） |
| `SYSTEM_AI_EDIT` | AI 二次编辑 | `{"html": 完整片段, "summary": …}` |
| `SYSTEM_EXTRACT_URL` | 从抓取正文抽取职位名 + 要求原文 | `{"job_title", "requirements"}`（不翻译、不改写） |
| `SYSTEM_INTERVIEW_QUESTIONS` | 生成 flashcard 问题 | `{"questions":[{topic,question,hint,placeholder}]}` |
| `SYSTEM_INTERVIEW_APPLY` | 资料页：回答→增量 | `{summary_append, achievements[], skill_notes[], projects_append[]}` |
| `SYSTEM_INTERVIEW_APPLY_CHAT` | 工作区：回答→增量（贴岗位关键词） | 同上 |
| `SYSTEM_PARSE_RESUME` | 粘贴简历文本→结构化资料 | 完整 profile JSON（缺字段填空） |

`SYSTEM_GENERATE` 由 `写作标准 A–F` + `硬约束 1–8` + `SPEC_CONTRACT` + `COVERAGE_CONTRACT` 组成。

### 2.1 `SYSTEM_GENERATE` 的「覆盖 vs 不编造」张力如何化解

这是全系统最核心的矛盾：**要覆盖岗位每一条要求**，但**不能为了覆盖而编造**。化解手段是**两档 source + 具体写法**：

- **覆盖要求**（硬约束 2）：`requirements`（含 responsibilities）**每条都必须产出一个覆盖项，不得留空、不得漏条**。
- **不编造**（硬约束 3）：`source` 只有两档——
  - `grounded`：来自真实经历，**必须**填 `evidence_ref`；
  - `capability`：资料无直接对应时，**只可**写「具备…能力 / 有…意向 / 可快速上手…」。
- **写法给了正面范例**（写作标准 A）：岗位要 Kubernetes、用户只有 Docker 经验时，示范如何写——
  > 「容器化与编排：具备 Docker 生产实践经验；熟悉 Kubernetes 核心概念（Pod/Service/Deployment）与声明式编排模型，可快速上手集群运维」

  既体现技术深度，又不谎称「在某公司做过 K8s」。
- **丰富度靠技术深度换**（标准 A/B/C）：资料有限时，把同一条经历从「技术选型 / 架构与实现 / 协作与流程 / 结果与影响」多角度展开，而不是编造新事实；没有量化数字时用**技术范围**替代（「覆盖 30+ 内部服务」）。

### 2.2 「严禁编造可证伪事实」的具体约束

`SYSTEM_GENERATE` 硬约束 4 明确列出**必须能在资料中溯源**的「具体可证伪的事实」：

> **公司名 / 时间 / 数字 / 学历 / 证书** —— 无法溯源的一律降级为 `capability`，并改写为不含具体事实的能力表述。

`SYSTEM_PARSE_RESUME`（`§ 最高优先级：绝不编造`）对**输入侧**做同样的约束：只提取文本里真实存在的信息，不根据常识补全（不因为写了「Python 工程师」就补出「精通 Flask」）。

### 2.3 禁止空话（标准 D）

「负责开发」「参与项目」「熟悉相关技术」「良好的沟通能力」「有责任心」等**单独出现即视为不合格**，必须替换为具体技术动作与范围。软技能必须绑定场景与技术。

---

## 3. 覆盖清单（coverage）契约

生成契约版（`COVERAGE_CONTRACT`，给模型看）：每个 `coverage[i]` 有 `req_id / requirement / section / resume_evidence / source / evidence_ref / note`。

代码侧收敛（`pipeline._normalize_coverage`）：

```
source 白名单 = {grounded, capability}
└─ 非法值 → 有 evidence_ref 则 grounded，否则 capability
└─ grounded 但无 evidence_ref → 强制降级 capability   （硬规则）
req_id 去重；requirements 中未覆盖的条目 → 自动补 capability + note
summary = {total, grounded, capability, uncovered}
```

- **`grounded` 必须有 `evidence_ref`**（格式 `experiences[i].achievements[j]` 等指向资料具体项）。
- **`capability` 的表达约束**：只写能力层面，严禁编造公司名/年限/项目名/数字/学历/证书。
- **coverage 绝不进正文**：存进 `Resume.coverage`，`renderer` 也会防御性剔除。

详见 [data-model.md §5](./data-model.md#5-resumecoverage-的-json-结构)。

---

## 4. 取消机制：为什么用 `should_cancel` 回调

`run_chat` 签名里的 `should_cancel: Callable[[], bool] | None`，由调度器注入 `lambda: self.is_cancelled(chat_id)`。

- **不能改成 `import scheduler`**：`scheduler.py` 顶部 `from .pipeline import TaskCancelled, run_chat`；若 `pipeline` 反过来 import `scheduler`，就形成**循环导入**（`pipeline ↔ scheduler`），启动即崩。
- **检查点**：每个阶段开始前调一次 `_check_cancelled()`，被取消则 `raise TaskCancelled`。
- **`TaskCancelled` 的语义**：与「失败」不同——调度器捕获它后**保持 `cancelled` 状态、绝不触发重试**（`scheduler._handle` 的 `except TaskCancelled` 分支）。
- **收尾窗口**：`run_chat` 成功返回后，`_handle` 会**再确认一次**取消状态，避免在收尾窗口内被取消却把状态覆盖成 `succeeded`。

---

## 5. 无 API Key 时的 mock 降级链路

`llm.LLMClient.enabled` = `bool(api_key.strip())`。为 False 时，`run_chat` 的 LLM 分支全部走 `mock`：

| 环节 | LLM 分支 | mock 分支（`mock.py`） |
|---|---|---|
| 岗位解析 | `client.chat_json(SYSTEM_ANALYZE, …)` | `mock.analyze_job(raw_text)`：分节识别（`_SECTION_HEADINGS`）+ 扁平 requirements + employer_questions 单列 + 技术栈/领域词/软技能词表命中 |
| 内容生成 | `client.chat_json(SYSTEM_GENERATE, …)` | `mock.generate_spec(profile, analysis)`：按关键词重合度排序经历/技能、`_expand_achievements` 只重组已有信息、`_skill_note` 生成深度线索（token 边界匹配避免 `go` 命中 `django`）、`_build_tagline` 用领域词拼技术定位 |
| 覆盖清单 | 模型产出 | `mock.heuristic_coverage`：按「需求关键词 ∩ 资料」判定 grounded/capability，`_find_evidence_ref` 生成 `evidence_ref` |
| AI 二次编辑 | `client.chat_json(SYSTEM_AI_EDIT, …)` | `mock.edit_html`：只支持少量指令（「删除技能章节」「切换深色」），否则原样返回 + 提示 |
| 简历解析 | `client.chat_json(SYSTEM_PARSE_RESUME, …)` | `mock.parse_resume_text`：正则/分块抽取（邮箱/电话/日期区间/技能分词/链接），**抽不到就留空，绝不编造** |
| 问答 | `client.chat_json(SYSTEM_INTERVIEW_*, …)` | `interview._profile_gaps_heuristic` / `_chat_gaps_heuristic`（关键词重合度） |
| 粘贴简历增补 | （同「简历解析」） | `profile_import.merge_profiles`：**只增补不覆盖**（标量填空缺、列表按身份键去重追加），`merge_summary` 统计新增条数 |

> mock 的定位是「**没有大模型时该怎么做**的参考实现」，保证整条链路（并发、进度、渲染、编辑、导出）真实跑通，而不是返回假数据。

**LLM 错误处理**：`LLMError` / `ValueError` 在 `run_chat` 里被包成 `RuntimeError("岗位解析失败：…" / "内容生成失败：…")` 向上抛给调度器 → 触发重试/置 failed。

**截断防御**（`llm.LLMClient.chat`）：`finish_reason == "length"` 时抛可读错误（区分「设了 max_tokens 被截断」与「服务端上限截断」），避免下游报出难以理解的「无法解析 JSON」。

---

## 6. 常见陷阱

1. **空资料直接抛错**：`_is_profile_empty`（姓名、经历、技能、教育全空）→ `RuntimeError(EMPTY_PROFILE_MSG)`，前端会看到「尚未填写资料库…」。生成前 `interview` 也做同样的检查。
2. **`coverage` 与 `spec` 分家**：`_normalize_spec` 返回的 dict 里带 `coverage`，但 `run_chat` 会 `pop` 出来存到 `Resume.coverage`，绝不写进 `spec.sections`。改这三处时别破坏这个约定。
3. **`MAX_RAW_TEXT = 20000`**：超长 JD 被截断后进 prompt，`analysis["truncated"]=True`，前端会提示「匹配分析可能未覆盖全部内容」。
4. **取消是「检查点式」而非「中断式」**：长耗时的 LLM 请求无法中途打断，取消在下一个检查点才生效；因此取消后排队的重试也必须先判断 `is_cancelled`。
5. **`run_task` 是兼容别名**：`pipeline.py` 与 `scheduler.py` 都保留了 `run_task = run_chat`，因为 `tests/regression.py` 通过 `sched_mod.run_task` 打桩注入「慢任务」。重命名时务必保留别名。

---

**相关文档**：[系统总览](./overview.md) · [数据模型](./data-model.md) · [任务调度器](./scheduler.md) · [安全设计](./security.md)
