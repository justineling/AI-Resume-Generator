# API 参考 / API Reference

> **这篇讲什么**：全部 HTTP 端点（方法 / 路径 / 入参 / 出参 / 错误码）+ 每个端点的 `curl` 示例 + 一组「约定与陷阱」。
> **适合谁读**：要接后端、写前端调用、或做接口联调的开发者。
>
> 权威来源：`backend/app/routers/*.py`、`backend/app/schemas.py`、`backend/app/main.py`。所有路径逐文件核对。
> **本地调试注意**：本机设了 `http_proxy`，访问 localhost 必须 `curl --noproxy '*'`（或 `NO_PROXY=localhost,127.0.0.1`）。

---

## 0. 通用约定

- **Base URL**：`http://127.0.0.1:8000`（默认）。前端同源调用，路径以 `/api` 开头。
- **错误体**：统一 `HTTPException(status, 中文 detail)`，响应体 `{"detail": "中文说明"}`。前端 `api.js` 用 `err.status` 区分 400/409，用 `detail` 弹 toast。
- **枚举**（`schemas.py`）：
  - `ChatStatus` / `TaskStatus` = `draft | queued | running | succeeded | failed | cancelled`
  - `ThemeId` = `ark | endfield | exa | corporate`
  - `LayoutId` = `single-column | sidebar-left | two-column | timeline | compact-grid`
  - `ChatMessageKind` = `jd | text | notice | guidance`
  - `EditScope` = `whole | section`
  - `CoverageSource` = `grounded | capability`
- **WS**：`/ws/tasks`（事件名见 [scheduler.md §7](../01-architecture/scheduler.md#7-与-websocket-事件的关系)）。

---

## 1. Health & 静态

| 方法 | 路径 | 出参 | 说明 |
|---|---|---|---|
| GET | `/api/health` | `{ok, llm_enabled}` | 读**运行时**配置（DB 覆盖 .env）判断是否启用 LLM |
| GET | `/` | `index.html` | 返回前端入口 |
| GET | `/static/*` | 静态资源 | `StaticFiles(directory=settings.static_dir)` |

```bash
curl --noproxy '*' -s http://127.0.0.1:8000/api/health
```

---

## 2. Profile（`/api/profile`）—— 全局资料库

| 方法 | 路径 | 入参 schema | 出参 schema | 错误 |
|---|---|---|---|---|
| GET | `` | — | `ProfileOut` | — |
| PUT | `` | `ProfileIn` | `ProfileOut` | — |
| POST | `/import-text` | `ProfileImportTextIn{text}` | `ProfileOut` | **400** 文本为空 |
| POST | `/interview` | `ProfileInterviewIn{focus}` | `InterviewQuestionsOut` | — |
| POST | `/interview/apply` | `InterviewAnswersIn{answers,auto_generate}` | `ProfileOut` | — |

- `_get_or_create`：首次访问自动建一条空 Profile。
- `PUT`：`payload.model_dump()` 全字段覆盖（**非部分更新**）。
- `/import-text`：LLM 优先解析（`SYSTEM_PARSE_RESUME`），无 Key 回落 `mock.parse_resume_text`；只写入 `ProfileIn` 白名单字段。
- `/interview`：`focus` 非空时构造 `analysis={"requirements":[{"id":"F1",...,"text":focus}]}` 引导方向。
- `/interview/apply`：`apply_answers(..., for_chat=False)`，**只增补**已有资料。

```bash
curl --noproxy '*' -s http://127.0.0.1:8000/api/profile
curl --noproxy '*' -X PUT http://127.0.0.1:8000/api/profile \
  -H 'Content-Type: application/json' -d '{"name":"张三","headline":"5 年后端工程师"}'
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/profile/import-text \
  -H 'Content-Type: application/json' -d '{"text":"张三\n5 年后端工程师\n工作经历…"}'
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/profile/interview \
  -H 'Content-Type: application/json' -d '{"focus":""}'
```

---

## 3. Chats（`/api/chats`）—— 聊天式工作区

### 3.1 URL 导入

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| POST | `/import-url` | `URLImportIn{url, auto_generate}` | `ChatOut`（201） | **422** 抓取失败 / 未能识别岗位要求 |

- `auto_generate` **仅作为前端行为提示**：后端**不直接入队**，避免绕过生成前的 AI 问答步骤（`chats.py` 注释）。前端 `chat.js` 固定传 `false`，导入后走 `generateChat()`。
- 抽取优先 LLM（`SYSTEM_EXTRACT_URL`），失败回落 `_heuristic_extract`。返回时 `out.extracted_title` 填 LLM 抽出的职位名。

### 3.2 CRUD

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| POST | `` | `ChatIn{title}` | `ChatOut`（201） | — |
| GET | `` | query `limit=100` | `list[ChatOut]` | — |
| GET | `/{chat_id}` | — | `ChatOut`（含 messages） | **404** |
| PATCH | `/{chat_id}` | `ChatPatch{title}` | `ChatOut` | **404** |
| DELETE | `/{chat_id}` | — | 空（**204**） | **404**；**400** 运行中/排队中 |

- 新建时对全局 Profile 做整体快照（`profile_snapshot`），`profile_dirty=False`。
- `GET ` 列表：`order_by(updated_at.desc())`，**不含 messages**（`messages` 留空）。
- `GET /{id}`：按 `created_at, id` 升序返回 messages。
- `DELETE`：级联删 Resume + EditLog + ChatMessage（见 [data-model.md §3](../01-architecture/data-model.md#3-关系与级联删除)）。

```bash
curl --noproxy '*' -s http://127.0.0.1:8000/api/chats
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/chats \
  -H 'Content-Type: application/json' -d '{"title":"高级后端工程师"}'
curl --noproxy '*' -X DELETE -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/chats/1
```

### 3.3 per-chat 资料快照

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| GET | `/{chat_id}/profile` | — | `ProfileOut`（伪造 id=chat_id） | **404** |
| PUT | `/{chat_id}/profile` | `ProfileIn` | `ChatOut` | **404** |
| POST | `/{chat_id}/profile/reset` | — | `ChatOut` | **404** |
| POST | `/{chat_id}/profile/import-text` | `ProfileImportTextIn{text}` | `ChatOut` | **404**；**409** 运行/排队中；**400** 文本为空 |

- `PUT` 全字段覆盖快照并置 `profile_dirty=True`。
- `reset` 重置为**当前**全局 Profile 并置 `profile_dirty=False`。
- `import-text`（工作区粘贴简历）：解析简历原文并**增补**进本对话快照（`profile_dirty=True`）。
  - **与资料页 `/api/profile/import-text` 语义相反**：资料页是**整体覆盖写库**，这里是**只增补不覆盖**（已有非空字段保留 → 用户手填 / 之前问答的更可信；列表字段按「身份键」去重后追加）。
  - 解析复用 `profile_import.parse_profile_text`（LLM 优先 / 回落 `mock.parse_resume_text`）；合并用 `profile_import.merge_profiles`（去重身份键见 `_IDENTITY`），新增条数统计用 `merge_summary`。
  - 会追加一条 `ChatMessage(kind="notice", content="已解析并写入本对话资料：…（只增补，不覆盖）")`，**不广播**（刷新后可见）。

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/chats/1/profile/import-text \
  -H 'Content-Type: application/json' -d '{"text":"张三\n工作经历\n…\n技能\nPython, Java"}'
```

### 3.4 消息

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| POST | `/{chat_id}/messages` | `ChatMessageIn{kind,content,meta}` | `ChatMessageOut`（201） | **404**；**400** `kind=jd` 但内容空 |
| GET | `/{chat_id}/messages` | — | `list[ChatMessageOut]` | **404** |

- `role` 由后端推导：`kind in ("jd","text")` → `user`，否则 `assistant`。
- `kind=jd` 时**同步写 `Chat.raw_text`** 并广播 `chat_message`。

### 3.5 生成 / 取消 / 重试 / 问答 / 覆盖清单

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| POST | `/{chat_id}/generate` | — | `ChatOut`（**202**） | **404**；**400** 无 JD；**409** 运行/排队中 |
| POST | `/{chat_id}/cancel` | — | `ChatOut` | **404** |
| POST | `/{chat_id}/retry` | — | `ChatOut` | **404**；**409** 运行/排队中 |
| POST | `/{chat_id}/interview` | `{}` | `InterviewQuestionsOut` | **404**；**409** 运行/排队中；**400** 无 JD / 空资料；**422** 解析失败 |
| POST | `/{chat_id}/interview/submit` | `InterviewAnswersIn` | `ChatOut` | **404**；**409** 运行/排队中；**400** 无 JD（当 `auto_generate`） |
| GET | `/{chat_id}/coverage` | — | `CoverageOut` | **404** 未生成简历 |

- `generate`：`scheduler.enqueue` 后 `session.refresh(chat)` 再返回（拿到 queued 状态）。
- `retry`：`attempts=0`、`error=""` 后 enqueue。
- `interview`：若 `job_analysis.requirements` 为空则**先解析一次**（不改变 chat 状态）。
- `interview/submit`：把回答 `apply_answers(..., for_chat=True)` 合并进快照（`profile_dirty=True`），写一条 `guidance` 消息；`auto_generate=True` 时 enqueue。
- `coverage`：取该 chat **最新一条** Resume 的 `coverage`。

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/chats/1/generate
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/chats/1/interview
curl --noproxy '*' -s http://127.0.0.1:8000/api/chats/1/coverage
```

---

## 4. Resumes（`/api/resumes`）

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| GET | `` | query `limit=100` | `list[ResumeMeta]` | — |
| GET | `/{resume_id}` | — | `ResumeOut` | **404** |
| PUT | `/{resume_id}` | `ResumeSaveIn{html,title,theme}` | `ResumeOut` | **404**；**400** 非法 theme |
| POST | `/{resume_id}/layout` | `LayoutIn{layout,theme}` | `ResumeOut` | **404**；**400** 无 spec / 非法 layout / 非法 theme |
| GET | `/{resume_id}/preview` | — | **完整 HTML 文档**（`HTMLResponse`，含 CSP） | **404** |
| GET | `/{resume_id}/download` | — | `text/html` 附件 | **404** |
| DELETE | `/{resume_id}` | — | 空（**204**） | **404** |

- `PUT`：只覆盖传入的非 None 字段，`version += 1`。**校验 theme**：非法 → 400（附可选值）。
- `layout`：基于保存的 `spec` 重渲染，**不调用 LLM**；更新 `spec` + `theme` + `html`。
- `preview`：`build_preview_document(r.html, r.title)`（转义标题 + 严格 CSP）。
- `download`：**RFC 5987 中文文件名**（见 §5）。
- `DELETE`：**同时删除关联 chat**（见 [data-model.md §3.2](../01-architecture/data-model.md#3-关系与级联删除)）。

```bash
curl --noproxy '*' -s http://127.0.0.1:8000/api/resumes/1
curl --noproxy '*' -X PUT http://127.0.0.1:8000/api/resumes/1 \
  -H 'Content-Type: application/json' -d '{"html":"<div class=\"resume\">…</div>"}'
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/resumes/1/layout \
  -H 'Content-Type: application/json' -d '{"layout":"two-column","theme":"exa"}'
curl --noproxy '*' -s http://127.0.0.1:8000/api/resumes/1/preview | head
curl --noproxy '*' -OJ http://127.0.0.1:8000/api/resumes/1/download   # 注意中文文件名
```

---

## 5. AI 二次编辑（`/api/resumes`，router tag=ai-edit）

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| POST | `/{resume_id}/ai-edit` | `AIEditIn{instruction,scope,target,current_html}` | `AIEditOut` | **404**；**400** 指令空 |
| GET | `/{resume_id}/edits` | — | `list[dict]`（最近 50 条，成功/失败都含） | — |
| POST | `/{resume_id}/undo` | — | `AIEditOut` | **404**；**400** 无可回退记录 |

- `ai-edit`：LLM 优先，无 Key → `mock.edit_html`。**入库前 `sanitize_fragment` 净化**；`removed` 为真时把「已自动净化」追加进 `summary`。每次编辑都写 `EditLog`（成功 `ok=True` 带 before/after，失败 `ok=False` 带 error）。
- `current_html` 优先于库内 html（前端未保存的最新内容）。
- `undo`：回退到**最近一次成功编辑**的 `before_html`。

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/resumes/1/ai-edit \
  -H 'Content-Type: application/json' -d '{"instruction":"把技能章节移到最前面","scope":"whole"}'
curl --noproxy '*' -s http://127.0.0.1:8000/api/resumes/1/edits
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/resumes/1/undo
```

---

## 6. Settings（`/api/settings`）

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| GET | `/providers` | — | `list[dict]`（纯数据） | — |
| GET | `/llm` | — | `LLMSettingsOut` | — |
| PUT | `/llm` | `LLMSettingsIn` | `LLMSettingsOut` | **400** `max_tokens < 0` |
| POST | `/llm/test` | `LLMTestIn` | `LLMTestOut` | **400** 未知供应商 / 未填 Base URL |

- `PUT /llm`：`provider_id` 与 `base_url` 保持一致（见 §7 陷阱 3）；`None` 字段保持不变。
- `GET /llm`：`key_hint` 脱敏（`sk-…a1b2` 形态），不返回明文 key。
- `/llm/test`：先 `GET {base_url}/models`；部分兼容端点未实现 `/models`（404/405）时回退一次最小 `chat/completions`（`max_tokens:1`）判 key 有效性。未填 key 时回落到**已存储 key（仅供应商一致时）**。`status_code` 到中文结论的映射见 `_fail`。

```bash
curl --noproxy '*' -s http://127.0.0.1:8000/api/settings/providers
curl --noproxy '*' -s http://127.0.0.1:8000/api/settings/llm
curl --noproxy '*' -X PUT http://127.0.0.1:8000/api/settings/llm \
  -H 'Content-Type: application/json' -d '{"provider_id":"deepseek","api_key":"sk-xxx","model":"deepseek-flash"}'
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/settings/llm/test \
  -H 'Content-Type: application/json' -d '{"provider_id":"deepseek","api_key":"sk-xxx"}'
```

---

## 7. Tasks（`/api/tasks`，**deprecated 兼容层**）

`router = APIRouter(prefix="/api/tasks", tags=["tasks"], deprecated=True)`。**内部一律映射到 Chat。**

| 方法 | 路径 | 入参 | 出参 | 错误 |
|---|---|---|---|---|
| GET | `` | query `limit=100` | `list[TaskOut]` | — |
| GET | `/{task_id}` | — | `TaskOut` | **404** |
| POST | `` | `JobTaskIn` | `TaskOut`（201） | **400** 无 raw_text |
| POST | `/batch` | `BatchTaskIn` | `list[TaskOut]`（201） | **400** jobs 为空 |
| POST | `/{task_id}/retry` | — | `TaskOut` | **404**；**409** 运行/排队中 |
| POST | `/{task_id}/cancel` | — | `TaskOut` | **404** |
| DELETE | `/{task_id}` | query `cascade=false` | 空（**204**） | **404**；**400** 运行中；**409** 已有简历且 `cascade=false` |

- `TaskOut` 派生字段：`company`（来自 `job_analysis.company`）、`resume_id`（左连 Resume）。
- `POST `` 与 `/batch` 会**直接 enqueue**（与 `/api/chats/{id}/generate` 不同——兼容层无问答步骤）。

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8000/api/tasks \
  -H 'Content-Type: application/json' -d '{"title":"后端","raw_text":"Requirements: Python…"}'
curl --noproxy '*' -X DELETE "http://127.0.0.1:8000/api/tasks/1?cascade=true" -o /dev/null -w '%{http_code}\n'
```

> **为什么 deprecated 仍保留**：`smoke.py` / `regression.py` / `progress_probe.py` / `xss_probe.py` / `e2e_verify.py` **5 个 HTTP 级测试**依赖它做安全网。删掉会失去回归覆盖。

---

## 8. 约定与陷阱

### 8.1 非法 layout/theme 返回 **400 而非 422**

`resumes.py::LayoutIn` 刻意用 `str` + 显式白名单校验，而**不是** pydantic `Literal`：

> Literal 会走 pydantic 校验返回 **422**，`detail` 变成**结构化列表**，前端 `api.js` 拼进 toast 会渲染成 JSON 串，可读性差。

所以「不支持的主题：xxx。可选值：ark, endfield, exa, corporate」这样一句中文 400，前端直接弹。

### 8.2 `max_tokens <= 0` = 不限制（请求体不发送该字段）；temperature 不发送

- `llm.LLMClient.chat`：`if self.max_tokens > 0: payload["max_tokens"] = self.max_tokens`。**`0` 是合法值（不限制）**，绝不能用 `or` 兜底成默认值（`get_runtime_config` 有注释强调）。
- **`temperature` 不发送**：`chat()` 的 payload 里没有 `temperature`，让模型服务端用自己的默认值。注意 `LLMSettingsIn.temperature` 字段存在但当前实现**不写入 payload**。
- `PUT /api/settings/llm` 仅 `max_tokens < 0` 拒绝（400）；`0` 接受。

### 8.3 `PUT /api/settings/llm` 不带 `provider_id` 时按 `base_url` 反查

若显式给了 `provider_id` → 用它；只给 `base_url` → `match_provider_by_base_url(base_url) or "custom"`；两者都没给 → 保持原值。**否则 DB 里旧的 `provider_id` 会变陈旧，与新的 `base_url` 自相矛盾**（`settings.py` 注释）。

### 8.4 中文文件名下载必须 RFC 5987

`resumes.py::download_resume`：

```python
quoted = quote(f"{filename}.html", safe="")
headers={"Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'}
```

提供 ASCII 回退 `filename="resume.html"` + `filename*=UTF-8''<urlquote>`。文件名中的 `/` `\` 被替换为 `_`，截断到 60 字符。

### 8.5 FastAPI 204 路由不能有响应体

204 端点必须用 `response_class=Response` 并 `return Response(status_code=204)`，否则 FastAPI 会因 response_model / 空体报错。涉及：` DELETE /api/chats/{id}`、`DELETE /api/resumes/{id}`、`DELETE /api/tasks/{id}`。

### 8.6 `/api/tasks*` deprecated 但有意保留

见 §7 末尾。

### 8.7 前端 `api.js` 的解析约定

- `res.status === 204` → 直接返回 `null`（不解析 body）。
- 非 2xx：`detail || message`，`err.status = res.status`（供 409 分支使用）。

---

## 9. 常见陷阱

1. **`generate` 返回 202 不是 200**：只表示「已入队」，真正的完成靠 WebSocket `chat_updated`。别轮询 `generate` 的返回等结果。
2. **`/import-url` 的 `auto_generate` 不能触发后端入队**：后端明确不 enqueue（会绕过问答）。要自动生成，前端在导入成功后自己调 `generateChat()`。
3. **`PATCH /api/chats/{id}` 只在 title 非空时生效**：传空串会被忽略（`if payload.title is not None and payload.title.strip()`）。
4. **`GET /api/chats` 不含 messages**：要消息得 `GET /api/chats/{id}` 或 `/{id}/messages`。
5. **错误码语义别混**：400 = 入参/状态不允许；**409 = 并发冲突（正在生成/已有简历）**；422 = 抓取/解析类失败（`/import-url`、`chat interview` 解析失败）。前端对 409 有专门提示。

---

**相关文档**：[系统总览](../01-architecture/overview.md) · [数据模型](../01-architecture/data-model.md) · [生成管线](../01-architecture/pipeline.md) · [安全设计](../01-architecture/security.md) · [前端模块](../04-frontend/modules.md)
