# AI Resume Generator

> 一个对话 = 一个求职场景 = 一份简历 → 粘贴岗位要求 → 生成针对性简历 → 所见即所得编辑 → 导出 PDF。
> 一个从零实现的全栈项目：**数据模型 / 任务调度 / 后端 API / 前端编辑交互** 全部覆盖。

---

## 目录

- [1. 它解决什么问题](#1-它解决什么问题)
- [2. 快速开始](#2-快速开始)
- [3. 目录结构](#3-目录结构)
- [4. 架构总览](#4-架构总览)
- [5. 数据模型](#5-数据模型)
- [6. 任务调度与并发](#6-任务调度与并发)
- [7. 后端 API 一览](#7-后端-api-一览)
- [8. 前端交互流程](#8-前端交互流程)
- [9. 关键技术取舍](#9-关键技术取舍)
- [10. 降级模式（无 API Key 也能跑）](#10-降级模式无-api-key-也能跑)
- [11. 测试](#11-测试)
- [12. 后续可扩展方向](#12-后续可扩展方向)
- [13. 已知限制](#13-已知限制)

---

## 1. 它解决什么问题

投递不同岗位时，同一份简历往往要反复改：措辞要贴合 JD、经历要重排、关键词要能过 ATS。
本项目把这件事流程化——**一个对话工作区就是一个求职场景，恰好产出一份简历**：

| 需求 | 实现位置 |
|---|---|
| ① 容错解析非结构化岗位文本 + 用户固定资料库 | `app/mock.py` `analyze_job()`、`app/models.py:Profile`、前端「资料」页 |
| ② LLM 分析岗位关键词与匹配点，生成针对性内容 | `app/llm.py`、`app/prompts.py`、`app/pipeline.py` |
| ③ 对话式工作区 + 后台并发 + 状态/进度/失败重试 | `app/scheduler.py`、`app/events.py`、`app/routers/chats.py`、前端「工作区」页 |
| ④ 版式由模型动态决定，不写死 | `app/prompts.py:SPEC_CONTRACT`、`app/renderer.py` |
| ⑤ HTML 承载，所见即所得 + 源码编辑 + 导出 PDF | `app/static/js/resume.js`、`app/routers/resumes.py` |
| ⑥ 自然语言 AI 二次编辑 | `app/routers/ai_edit.py`、`app/prompts.py:SYSTEM_AI_EDIT` |
| ⑦ 生成时产出覆盖清单（每条岗位要求都有对应表述） | `app/pipeline.py`、`Resume.coverage`、前端「匹配分析」tab |

---

## 2. 快速开始

### 一键启动（推荐）

```bash
python start.py
```

就这一条。启动器会自动完成：**建虚拟环境 → 装依赖 → 挑可用端口 → 起服务 → 打开浏览器**。
首次运行需要 1-2 分钟装依赖，之后每次启动都是秒开。

不想敲命令的话：

| 系统 | 做法 |
|---|---|
| Windows | 双击 `start.bat` |
| macOS / Linux | `./start.sh`（首次需 `chmod +x start.sh`） |

**常用参数**：

```bash
python start.py --port 9000      # 换端口（被占用时也会自动顺延）
python start.py --no-browser     # 不自动打开浏览器
python start.py --reload         # 开发模式：改代码自动重启
python start.py --reinstall      # 强制重装依赖
```

启动器还会顺手处理一个容易踩的坑：**若系统设置了 `HTTP_PROXY`，代理会拦截 localhost 请求（表现为 502）**，
导致本地 LLM（如 Ollama）和本地服务都不可用。脚本会为服务进程设置 `NO_PROXY`，本地地址自动绕过代理。

> **无需 API Key 也能直接启动** —— 会进入降级模式，用启发式规则完成同样的流水线，
> 保证并发、进度、渲染、编辑、导出这些工程能力都能真实跑通。详见 [第 10 节](#10-降级模式无-api-key-也能跑)。

### 手动启动（想自己控制每一步时）

```bash
# 1) 创建虚拟环境并安装依赖（Python 3.11+）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt     # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS / Linux

# 2) 配置模型（可选，留空则使用本地降级模式）
cp .env.example .env
#   编辑 .env，填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

# 3) 启动
.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000> 即可。API 文档在 <http://127.0.0.1:8000/docs>。

**推荐模型配置**（任意 OpenAI 兼容服务均可；也可在页面右上角「设置」里选供应商 + 填 Key，改完立即生效）：

```env
# DeepSeek（默认推荐）
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY=sk-xxx

# 阿里通义
# LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# LLM_MODEL=qwen-plus
```

### 模型配置（供应商预设）

页面右上角「设置」里 **选供应商 → 填 API Key → 点「测试连接」→ 选模型**，全程**不需要手填 Base URL**。
内置 10 个供应商预设，选中后 Base URL 与模型列表自动填充：

| 供应商 | Base URL | 预设模型（示例） |
|---|---|---|
| **DeepSeek（默认）** | `https://api.deepseek.com` | `deepseek-flash`（性价比）/ `deepseek-v4-pro`（更强） |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` / `gpt-4o-mini` |
| Moonshot Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` / `-32k` / `-128k` |
| 阿里云百炼（通义） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` / `qwen-turbo` / `qwen-max` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` / `glm-4-flash` |
| 火山方舟（豆包） | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-pro-32k` / `doubao-lite-32k` |
| 腾讯混元 | `https://api.hunyuan.cloud.tencent.com/v1` | `hunyuan-turbo` / `hunyuan-pro` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` 等 |
| Ollama（本地） | `http://localhost:11434/v1` | `llama3.1` / `qwen2.5`（无需 Key） |
| 自定义 | 自行填写 | 自行填写 |

**DeepSeek**：Base URL `https://api.deepseek.com`（OpenAI 兼容，`/chat/completions` 直接拼接）；
API Key 申请 <https://platform.deepseek.com/api_keys>；文档 <https://api-docs.deepseek.com/>。

**「测试连接」**：调用 `GET {base_url}/models` 校验 Key（`200` = 有效、`401` = 无效）；
若该端点不存在（`404`/`405`）会自动**回退发一次最小 `/chat/completions`** 探测。
一次点击同时完成「校验 Key」与「拉取真实模型列表刷新下拉框」。

**模型下拉框**：切换供应商时用预设模型自动填入，测试成功后用真实列表刷新；
预设名可能随服务商版本变化，故始终提供「＋ 自定义模型名…」兜底。

**生效范围**：设置存于 `AppSetting` 表并**覆盖 `.env`**，保存后立即作用于后续所有生成任务，无需重启。

**输出长度（`max_tokens`）**：**默认 0 = 不限制**——此时不向 API 发送 `max_tokens` 字段，
由服务端用自己的默认 / 最大上限决定（用户不介意 token 消耗）。填正数则显式指定上限。
AI 二次编辑需要模型返回整份简历 HTML（约 9KB），若显式设了过小的值会导致输出被截断。
程序会检测 `finish_reason == "length"` 并提示「输出被截断」，而不是抛出难懂的「无法解析 JSON」。
设置页可调：`0` 合法（不限制），负数非法（400）。

**代理环境**：应用会读取 `HTTP_PROXY` / `http_proxy` 等环境变量，但**对本机地址
（`localhost` / `127.0.0.1` / `::1`）自动绕过代理**，保证 Ollama 等本地 LLM 可用；
访问外部供应商时仍走代理。

---

## 3. 目录结构

```
AI Resume Generator/
├── start.py                       # ★ 一键启动：建环境 → 装依赖 → 起服务 → 开浏览器
├── start.bat                      # Windows 双击启动
├── start.sh                       # macOS / Linux 启动
├── requirements.txt
├── .env.example
├── docs/                          # 设计文档
│   ├── system_design.md           # 系统设计（含类图 / 时序图）
│   ├── class-diagram.mermaid
│   └── sequence-diagram.mermaid
├── data/                          # SQLite 数据库（运行时生成）
└── backend/
    ├── run.py                     # 开发入口
    ├── tests/
    │   ├── smoke.py               # 后端端到端冒烟测试
    │   ├── regression.py          # 缺陷回归测试（P0/P1/P2 修复项）
    │   ├── chat_e2e.py            # 聊天式工作区端到端（需服务在跑）
    │   ├── ui_check.py            # 前端 UI 自动化验证（Playwright + 系统 Edge）
    │   ├── e2e_verify.py          # 端到端验证（浏览器层 + 安全边界）
    │   ├── xss_probe.py           # XSS 端到端
    │   ├── settings_ui_check.py   # 模型配置界面实测
    │   └── progress_probe.py      # 进度归零采样
    └── app/
        ├── main.py                # FastAPI 入口：路由挂载 / 生命周期 / WebSocket
        ├── config.py              # 配置（env / .env）
        ├── db.py                  # 引擎与会话（SQLite + WAL）
        ├── migrate.py             # 幂等迁移：旧库（JobTask / 无快照）→ 新模型
        ├── models.py              # SQLModel 数据模型（Profile / Chat / ChatMessage / Resume / EditLog / AppSetting）
        ├── schemas.py             # 请求/响应模型 + 素材结构约定
        ├── llm.py                 # OpenAI 兼容客户端 + JSON 容错解析 + 截断检测
        ├── providers.py           # LLM 供应商预设注册表（DeepSeek/OpenAI/… 共 10 家）
        ├── prompts.py             # 提示词模板（解析 / 生成 / 二次编辑）
        ├── mock.py                # 本地降级生成器（启发式）
        ├── renderer.py            # spec → 自包含 HTML（Jinja2 + 版式 CSS）
        ├── sanitize.py            # HTML 净化器：nh3 白名单净化 + style 块 @import 剥离
        ├── pipeline.py            # 单对话生成流水线
        ├── scheduler.py           # asyncio 队列 + worker 池 + 指数退避重试
        ├── events.py              # WebSocket 广播中心
        ├── routers/               # profile / chats / tasks(兼容层) / resumes / ai_edit / settings
        ├── templates/             # （预留）服务端模板
        └── static/                # 前端 SPA
            ├── index.html
            ├── css/app.css        # Ark UI 设计语言（ark 家族 / moderate 深度）
            └── js/{api,ui,profile,chat,resume,app}.js
```

---

## 4. 架构总览

```
┌─────────────── 浏览器 SPA (Ark UI) ───────────────┐
│  资料库  │  工作区(聊天式)  │  简历工作台           │
└────┬──────────┬───────────────┬──────────────────┘
     │ REST     │ REST + WS     │ REST
┌────▼──────────▼───────────────▼──────────────────┐
│                   FastAPI                         │
│  routers/  ──  profile / chats / resumes /        │
│                ai_edit / settings                 │
│                （tasks 为 deprecated 兼容层）      │
├───────────────────────────────────────────────────┤
│  TaskScheduler   asyncio.Queue + N workers（键=chat id）
│    └─ pipeline.run_chat()                         │
│         10% 载入资料快照 → 25% 解析岗位 →         │
│         60% 规划版式+生成内容 → 85% 渲染 → 100%   │
├───────────────────────────────────────────────────┤
│  LLMClient (OpenAI 兼容)  ⇄  远端大模型            │
│    ↳ 未配置 key 时降级到 mock.py 启发式            │
├───────────────────────────────────────────────────┤
│  renderer:  spec(JSON) → 自包含 HTML              │
│  EventHub:  WebSocket 广播 chat_* 进度事件         │
└──────────────┬────────────────────────────────────┘
               │ SQLModel / SQLite (WAL)
        ┌──────▼──────┐
        │  app.db     │  Profile / Chat / ChatMessage / Resume / EditLog / AppSetting
        └─────────────┘
```

---

## 5. 数据模型

`backend/app/models.py`

| 表 | 作用 | 关键字段 |
|---|---|---|
| **Profile** | 全局资料库（主资料，全局单份） | 标量字段 + `education / experiences / projects / skills / certificates / languages / awards / links`（JSON 列）+ `avatar`（data-url） |
| **Chat** | 一个求职场景 = 一份简历的工作单元（调度最小单位） | `title` / `status`(draft/queued/running/succeeded/failed/cancelled) / `stage` / `progress` / `raw_text` / `profile_snapshot`(JSON) / `profile_dirty` / `job_analysis`(JSON) |
| **ChatMessage** | 对话消息 | `chat_id` / `role`(user/assistant/system) / `kind`(jd/text/notice/guidance) / `content` / `meta`(JSON) |
| **Resume** | 生成结果 | `chat_id`(FK，**单向**) / `html`（**唯一真相来源**）/ `spec`（生成时结构快照）/ **`coverage`**（覆盖清单 JSON）/ `theme` / `version` |
| **EditLog** | AI 二次编辑历史（含"重新生成"） | `instruction` / `scope` / `target` / `before_html` / `after_html` / `ok` |
| **AppSetting** | 运行时可改的配置 | `key` / `value`（覆盖 `.env`） |

**关联收敛为单向**：只保留 `Resume.chat_id`，对话列表所需的 `resume_id` 由查询左连派生，
不再存储 `Chat.resume_id`，避免双向引用带来的删库绕外键代码。

**为什么用整体快照（`Chat.profile_snapshot`）而不是增量 diff？**

对话是一个**冻结的工作上下文**：新建对话时对全局 Profile 做一次整体深拷贝，
对话内修改只影响本对话，不影响全局、也不影响其它对话。相比"记一组增删改差异"，
整体快照的收益是：读取时无需重放 diff（直接就是可用资料）、语义简单不易出错。
而"重置回全局"退化为一次深拷贝即可，不需要维护撤销栈。

**为什么用 JSON 列而不是把每条经历拆成独立的表？**
简历素材是"整体读写、整体喂给模型"的，没有按单条经历做关联查询的需求。
拆表会带来大量 JOIN 和 N 张表的迁移负担，收益接近于零。用 JSON 列换来了：
新增字段（比如"发表的论文"）零迁移、直接透传给模型。代价是无法用 SQL 做细粒度筛选——
这在当前场景里不重要。

**素材结构约定**（前端表单、提示词、渲染器三方共享，见 `schemas.py` 顶部注释）：

```jsonc
// experiences 元素
{ "company": "ACME", "title": "高级后端工程师", "start": "2021.03", "end": "至今",
  "location": "上海", "summary": "负责订单核心链路重构。",
  "achievements": ["把核心接口 P99 从 800ms 降到 120ms"], "tech": ["Python", "K8s"] }
```

---

## 6. 任务调度与并发

`backend/app/scheduler.py`

```
enqueue(chat_id) ──► asyncio.Queue ──► worker #0 ─┐
                                      worker #1 ─┤─► pipeline.run_chat()
                                      worker #2 ─┘
```

**关键设计**

1. **DB 是队列的真相来源，内存队列只是触发器。**
   对话的真实状态写在 `Chat.status`。进程内 `asyncio.Queue` 只负责"叫醒"worker。
   因此进程重启后，`_recover()` 能把残留的 `running` / `queued` 对话重新捞回队列继续跑。

2. **并发度可运行时调整。** 设置页改 `worker_count` → `_watch_settings()` 每 5 秒轮询一次，
   发现变化就平滑取消旧 worker 协程并拉起新的，不需要重启服务。

3. **进度与状态实时外推。** 流水线每个阶段都写 DB 并 `EventHub.broadcast()`，
   前端通过 `/ws/tasks` 收到 `chat_progress` / `chat_updated` 事件（payload 带 `chat_id`），
   就地更新对话，无需轮询。

4. **失败重试 = 指数退避 + 上限。**
   `attempts <= max_retries` 时按 `retry_backoff × 2^(attempts-1)` 秒延迟重新入队（封顶 60s）；
   超限则置 `failed` 并保留错误详情。手动「重试」会把 `attempts` 归零，给用户干净的重新开始。
   对话的「抢占」用条件 `UPDATE ... WHERE status='queued'` 原子完成，重复入队不会被两个 worker 同时处理。

5. **取消是真正生效的。** `cancel()` 把 chat id 放进 `_cancelled` 集合并改写状态；
   `pipeline.run_chat()` 在每个阶段开始前通过注入的 `should_cancel` 回调检查该集合，
   一旦取消即抛出 `TaskCancelled`，调度器捕获后**保持 `cancelled` 状态、不进入失败重试**。
   因此取消运行中的对话会真正中止它，不会被跑完覆盖成 `succeeded`。

**流水线阶段与进度映射**（`pipeline.py`）

| 进度 | 阶段 | 说明 |
|---|---|---|
| 10% | 载入对话资料快照 | 读取 `Chat.profile_snapshot`（而非全局 Profile） |
| 25% | 解析岗位要求 | LLM `SYSTEM_ANALYZE` 或 `mock.analyze_job()`（识别分节 + 单独抽出雇主提问） |
| 60% | 规划版式并生成内容 | LLM `SYSTEM_GENERATE` 或 `mock.generate_spec()`，同时产出覆盖清单 |
| 85% | 渲染简历版式 | `renderer.render_resume_html(spec)` |
| 100% | 完成 | 落库 Resume（含 `coverage`），回填 `chat_id` |

---

## 7. 后端 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（返回运行时配置：`llm_enabled`） |
| GET / PUT | `/api/profile` | 读取 / 保存**全局**资料库 |
| POST | `/api/chats` | 新建对话（创建时快照全局资料） |
| GET | `/api/chats` | 对话列表（含派生的 `resume_id`） |
| GET | `/api/chats/{id}` | 对话详情 + 消息列表 |
| PATCH | `/api/chats/{id}` | 改标题 |
| DELETE | `/api/chats/{id}` | 删除对话（级联删 Resume + EditLog + ChatMessage；运行中返回 400） |
| GET / PUT | `/api/chats/{id}/profile` | 读写**该对话的资料快照**（不影响全局） |
| POST | `/api/chats/{id}/profile/reset` | 快照重置回全局资料 |
| POST / GET | `/api/chats/{id}/messages` | 追加 / 读取对话消息 |
| POST | `/api/chats/{id}/generate` | 触发生成（无 JD→400，已在运行→409） |
| POST | `/api/chats/{id}/cancel` | 取消 |
| POST | `/api/chats/{id}/retry` | 重试 |
| GET | `/api/chats/{id}/coverage` | 覆盖清单（未生成→404） |
| GET | `/api/resumes` | 简历列表（元信息，含 `chat_id`） |
| GET | `/api/resumes/{id}` | 简历全文（`html` + `spec` + `coverage`） |
| PUT | `/api/resumes/{id}` | 保存编辑结果（HTML 为真相来源） |
| POST | `/api/resumes/{id}/layout` | 按 spec 重排版 / 换主题（**不消耗 LLM**） |
| POST | `/api/resumes/{id}/ai-edit` | **自然语言二次编辑** |
| GET | `/api/resumes/{id}/edits` | 编辑历史 |
| POST | `/api/resumes/{id}/undo` | 回退到上次编辑前 |
| GET | `/api/resumes/{id}/preview` | 完整 HTML 文档（iframe 预览 / 打印） |
| GET | `/api/resumes/{id}/download` | 下载独立 `.html`（RFC 5987 中文文件名） |
| DELETE | `/api/resumes/{id}` | 删除简历，**同时删除所属对话**（一对一单元） |
| GET / PUT | `/api/settings/llm` | 读取 / 保存模型与并发设置（含 `provider_id` / `max_tokens`） |
| GET | `/api/settings/providers` | 返回 LLM 供应商预设列表（id / label / base_url / 预设模型 / 申请 Key 链接 / 文档链接） |
| POST | `/api/settings/llm/test` | 校验 API Key 有效性并尽力返回真实模型列表；入参 `{provider_id, base_url?, api_key, model?}`；失败也返回 200 + `ok=false` + 可读中文 message |
| WS | `/ws/tasks` | 对话进度事件流（事件名 `chat_message` / `chat_progress` / `chat_updated`，payload 带 `chat_id`） |

> **`/api/tasks*` 是 deprecated 兼容层**：仅为向后兼容旧前端与既有 HTTP 级测试（smoke / regression /
> progress_probe / xss_probe / e2e_verify）而保留，内部一律映射到 Chat
> （`POST /api/tasks`、`/api/tasks/batch` → 各建一个 Chat 并入队）。**新代码请使用 `/api/chats`**，
> 本路由后续会下线。

---

## 8. 前端交互流程

```
① 资料页  ── 填基本资料 / 头像 / 经历 / 技能（可重复条目，支持 JSON 导入导出）
             ↓
② 工作区  ── 「＋ 新建」创建对话（创建时对全局资料做整体快照）
             ├ 在对话内确认 / 微调资料快照（可「重置为全局」，不影响全局资料）
             ├ 粘贴岗位要求 → 「贴为 JD」
             └ 点「生成简历」→ 对话流内实时看进度（chat_progress / chat_updated）
             ↓
③ 工作台  ── 左侧简历列表 / 右侧五个 tab：
             ├ 所见即所得：iframe(designMode) 直接改文字与顺序
             ├ HTML 源码：直接改标记，一键回填编辑器
             ├ AI 修改：自然语言指令 → 模型返回新 HTML → 立即生效；支持回退
             ├ 版式：切换 5 种布局（single-column / sidebar-left / two-column / timeline / compact-grid，均生效）
             │        + 4 套主题（基于 spec 重渲染，不调 LLM）
             └ 匹配分析：渲染**覆盖清单**——岗位每条要求都有对应表述，徽章区分
                        `资料支撑`（绿，真实经历支撑，带 evidence_ref）/ `能力表述`（青）
             ↓
          保存 / 打印 PDF / 下载 HTML
```

导航为 **01 资料 / 02 工作区（默认）/ 03 简历**，已移除旧的「岗位」页与「任务」看板。
岗位解析识别出的 `Employer questions`（申请表提问）在对话内单独折叠展示，**不进入简历正文**。

**所见即所得是怎么做的？**
把简历片段（`<style>` + `.resume` 结构）通过 `iframe.srcdoc` 注入 iframe，加载完设
`contentDocument.designMode = 'on'`。用 iframe 而不是 `contenteditable` div 的原因很关键：
简历的 CSS 里有 `:root`、`@page`、`.resume` 这类全局选择器，直接塞进主文档会污染整个应用的样式；
iframe 天然做了样式隔离，同时保留了浏览器原生的编辑能力。

---

## 9. 关键技术取舍

### ① 模型决定"结构与内容"，渲染器决定"HTML"

模型输出的不是 HTML，而是一份 **resume spec JSON**（版式 + 章节顺序 + 每章内容 + 强调等级），
再由确定性的 Jinja2 渲染器转成 HTML。

- ✅ 版式依然由模型动态决定（它选 `layout`、排章节、标 `emphasis`），满足"不写死"的要求
- ✅ **生成路径**：所有文本经 Jinja2 autoescape 转义，模型无法把 XSS 写进简历
- ✅ **编辑路径**：AI 二次编辑返回的 HTML 入库前先经 `sanitize.py`（基于 **nh3 / ammonia**）
  做白名单净化，剥离脚本 / 事件属性 / 危险协议，避免存储型 XSS
- ✅ **架构级兜底**：`/preview`、`/download` 与打印视图的文档都带严格 CSP
  （`default-src 'none'`）——即便净化器被绕过，注入的脚本也无法执行
- ✅ 样式集中在一处，改一次全站生效；模型换模型不会导致版式崩坏
- ✅ spec 落库后，用户切换版式/主题可以**零 LLM 成本**重渲染
- ⚠️ 代价：模型不能"自由发挥"出渲染器没定义的版式。这是刻意的收敛——
  简历是给人看的正式文档，可控性比自由度重要。想放开的话，可以给渲染器加新 `type`。

### ② HTML 作为简历的唯一真相来源

编辑、AI 修改、导出都围绕 HTML 进行，spec 只作为"生成时的快照"保留。

- ✅ 所见即所得编辑、源码编辑、AI 改 HTML、打印，四条路径天然统一，不需要在
  "结构化模型 ↔ 渲染结果"之间反复做有损转换
- ✅ 导出/下载天然可行（HTML 自带样式，可离线打开）
- ⚠️ 代价：用户在编辑器里手动改了结构后，spec 就与 HTML 不同步了。
  此时「版式」页的重渲染会覆盖手改内容——所以 UI 上明确标注了这是"基于结构快照重新渲染"。

### ③ asyncio 队列而不是 Celery / Redis

- ✅ **零外部依赖**，`pip install -r requirements.txt` 后一条命令就能跑起来，
  符合"可运行的项目实现"这个目标
- ✅ 并发模型与 FastAPI 同源（同一个事件循环），没有跨进程序列化的开销和复杂度
- ⚠️ 代价：无法多机横向扩展，任务不能跨进程分发。
  **升级路径是清晰的**：`scheduler.enqueue()` 换成 `celery_app.send_task()`，
  `pipeline.run_chat()` 原样搬进 Celery task 即可，业务代码几乎不用动；
  进度外推从进程内 `EventHub` 换成 Redis Pub/Sub。

### ④ 前端用原生 ES Module，不上构建工具

- ✅ 无 `npm install`、无打包步骤，`index.html` 直接 `type="module"` 加载 6 个 JS 文件
- ✅ 代码可读性即工程性，评审时不用先理解构建配置
- ⚠️ 代价：没有 HMR、没有 tree-shaking。对于这个体量（~1500 行前端）是划算的；
  再大就该上 Vite 了。

### ⑤ PDF 导出走浏览器打印，而不是服务端渲染

- ✅ 文字是**矢量**的，可选中、可搜索、体积小；中文字体直接用系统字体，不用打包字体文件
- ✅ 不需要 Playwright / WeasyPrint 这类重依赖（省掉几百 MB 浏览器或字体依赖）
- ⚠️ 代价：无法在服务端批量生成 PDF。若需要（例如批量投递），
  可以加一个 Playwright 无头页面加载 `/api/resumes/{id}/preview` 再 `page.pdf()`——
  预览视图这个接口已经为这条路准备好了。

### ⑥ 提示词里反复强调"不编造"

简历是**会对用户产生真实后果**的文档。所以 `SYSTEM_GENERATE` 里明确要求：
只使用用户资料中真实存在的信息，可以重组措辞、重排优先级、按 JD 关键词改写，
但**绝不编造学历、公司、时间、数字**。这比让模型"写得更漂亮"重要得多。

### ⑦ 降级模式是产品能力，不是兜底

`mock.py` 不是"LLM 不可用时的报错"，而是一条完整的、确定性的启发式实现：
按关键词重合度给经历和技能排序、按有无匹配技能决定章节顺序、抽取 bullet 作为硬性要求。
它保证了**工程链路（并发/进度/渲染/编辑/导出）可以被独立验证**——
`smoke.py` 和 `ui_check.py` 就是跑在这条路径上的。

### ⑧ 一个 chat 一个简历，重新生成 = 覆盖

覆盖而非保留版本历史，是因为「一 chat 一简历」是明确的业务约束，
保留多版本会让 chat 与简历的关系变模糊。
但覆盖**不是不可逆的**：每次重新生成都会写一条 `EditLog(instruction="重新生成", before_html, after_html)`，
复用现有的「回退上一版」机制。这样既守住了业务约束，又没有丢掉可恢复性。

---

## 10. 降级模式（无 API Key 也能跑）

未配置 `LLM_API_KEY` 时，`build_client().enabled == False`，流水线自动改走 `mock.py`：

| 环节 | 降级实现 |
|---|---|
| 岗位解析 | 按 bullet/句子切分，词表匹配技术栈与软技能，词频统计关键词，剔除 JobStreet/Indeed 的导航噪音 |
| 匹配排序 | 用关键词重合度给经历、技能打分并重排，命中的标 `emphasis=high` |
| 版式规划 | 固定 `sidebar-left`，技能命中则技能前置 |
| 覆盖清单 | **同样产出**：用关键词重合度判定每条要求是 `grounded`（有对应经历/技能支撑）还是 `capability`（仅能力层面表述），不是空壳 |
| 二次编辑 | 支持少量关键词指令（如"切换深色""删除技能章节"），其余情况明确告知需接入 LLM |

前端顶栏会显示 `LLM // 降级模式`，一眼可辨。

---

## 11. 测试

> **建议用隔离库跑，避免测试数据写入你的正式库：**
> `DATABASE_URL="sqlite:///./data/test.db" python backend/tests/regression.py`
> 不带 `DATABASE_URL` 时会打到共享库 `data/app.db`（可能与正在运行的服务争抢同一文件），
> 进程内测试脚本会在开头打印一行醒目警告。`xss_probe.py` / `settings_ui_check.py` 自起
> 隔离库，无需手动指定。

```bash
# 后端端到端：资料 → 建对话 → 并发生成 → AI 编辑 → 重排版 → 导出
.venv/Scripts/python.exe backend/tests/smoke.py

# 缺陷回归：级联删除 / 取消 / XSS 转义 / 版式 / 非法枚举 / 空资料 / 净化 / 退避 / 供应商预设 / 对话模型 等（209 项）
.venv/Scripts/python.exe backend/tests/regression.py

# 聊天式工作区端到端：新建对话 → 快照 → 贴 JD → 生成 → 覆盖清单 → 级联删除（需先启动服务）
.venv/Scripts/python.exe backend/tests/chat_e2e.py

# 进度归零采样：人工确认重排队与最终失败时 progress 均归零（自带 TestClient，无需起服务）
.venv/Scripts/python.exe backend/tests/progress_probe.py

# XSS 端到端：无头 Edge 加载 /preview 断言注入脚本不执行（12 项）
#   自起隔离 DB + 独立端口（8123）的 uvicorn 子进程，无需另起服务
.venv/Scripts/python.exe backend/tests/xss_probe.py

# 前端 UI 自动化（Playwright + 系统 Edge，无需下载浏览器）
#   需先启动服务，会截图到 backend/tests/shots/ 并收集控制台错误
.venv/Scripts/python.exe backend/tests/ui_check.py

# 模型配置界面实测：供应商切换 / Base URL 自动填充 / 测试连接 / 保存后顶栏（31 项）
#   自起隔离 DB + 独立端口（8124），无需另起服务
.venv/Scripts/python.exe backend/tests/settings_ui_check.py

# 端到端验证（浏览器层 + 安全边界：CSP 兜底 / 版式几何 / API 契约，41 项），需先启动服务
.venv/Scripts/python.exe backend/tests/e2e_verify.py
```

`ui_check.py` 覆盖：填资料 → 新建对话 → 贴 JD → 等待并发生成 → 打开简历 →
遍历五个编辑 tab → 执行 AI 修改 → 打开设置 → 移动端视口。

---

## 12. 后续可扩展方向

- **多份资料库**：`Profile` 加 `is_default` / 多行，支持"技术版 / 管理版"两套素材
- **JD 抓取**：加一个 URL 输入框，后端抓取正文后复用现有解析链路
- **服务端 PDF**：用 `/api/resumes/{id}/preview` + Playwright 批量出 PDF
- **简历打分**：再跑一次 LLM 对生成的简历做"ATS 命中率 / 匹配度"评分并给出改进建议
- **向量检索**：资料库变大后，先按 JD 做 RAG 召回相关经历，再交给模型，降低 token 成本
- **多用户**：加认证后把 `Profile` / `Chat` 挂到 `user_id`，前端加登录态

---

## 13. 已知限制

如实列出当前实现的边界，避免把能力说得比实际更强：

- **无鉴权**：仅面向本机单用户场景，API 不做身份校验，默认只监听 `127.0.0.1`。请勿直接暴露到公网。
- **多进程部署下的恢复**：`_recover()` 在启动时把残留的 `running` 对话重新入队。若用多个 uvicorn worker 同时跑，每个进程都会执行恢复，同一对话可能被重复处理。当前设计假设单进程运行。
- **无分页**：`/api/chats`、`/api/resumes` 只支持 `limit` 上限，没有游标分页；数据量大时列表会变慢。
- **真实 LLM 路径未做端到端实测**：本项目在无 API Key 的降级模式（`mock.py`）下通过了全部自动化测试；接入真实模型（OpenAI 兼容接口）的链路已实现但未在 CI 中做端到端验证，首次接入请自行核对返回结构。
- **非 DeepSeek 供应商的预设模型名可能过时**：内置预设（除 DeepSeek 外）取自公开文档，可能随服务商版本变化，请以「测试连接」拉取的真实列表为准；真实外部供应商的连通性同样未做端到端实测（无真实 Key）。
- **净化为白名单式**：`sanitize.py` 使用成熟库 **nh3**（ammonia）做标签/属性白名单净化（白名单覆盖版式/表格/代码等安全结构标签），并在其后剥离 `<style>` 块内的 `@import` 外链 at-rule，避免外链被浏览器请求形成探测面；`/preview`、`/download` 与打印文档另加了严格 CSP（`default-src 'none'`）作为架构级兜底。用户手动 `PUT` 的 HTML 不做净化（属自伤，非威胁模型），但同样受 CSP 保护。
- **状态枚举无 DB 约束**：`status` / `scope` / `theme` 等在 DB 层是自由字符串，仅在 API 入参层用 `Literal` 收敛；直接写库仍可能绕过。
- **兼容层 `/api/tasks*` 是过渡产物**：内部映射到 Chat。等测试全部迁到新接口后应下线，避免双模型维护成本。
- **`capability` 档仍可能被误读为编造**：提示词里已写死「不得编造公司名/年限/项目名/数字/学历」，前端也用青色徽章标注「能力表述，非具体事实」，但仍建议投递前自己过一遍覆盖清单。
- **旧数据迁移时 `profile_snapshot` 用「迁移当刻的全局资料」**：旧库没存快照，无法还原原始生成时点的资料，迁移日志会说明。

---

## 设计说明

界面采用 **Ark UI 设计语言**（`ark` 家族 / `moderate` 应用深度）：
近黑底 + 1px 白色细线 + 青色信号色 `#18d1ff`、草绿状态色 `#c8eb21`、
等宽字体承载数据与编号、方形几何与面板左上角折角标记、克制的方向性进场动效。
所有几何与纹理均为原创 CSS/SVG，未使用任何受保护的第三方素材。
