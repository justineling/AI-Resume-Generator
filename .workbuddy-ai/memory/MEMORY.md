# AI Resume Generator — 项目长期记忆

## 项目定位
基于岗位要求（JobStreet / Indeed 粘贴文本）自动生成针对性简历的全栈应用。
本地运行、数据不出本机。

## 业务模型（第五轮重构后，这是最重要的一条）
**一个 chat = 一个求职场景 = 恰好一份简历。** 像网页版 LLM 一样的聊天式工作区。
- 用户在 chat 内：确认/微调个人资料 → 粘贴岗位要求 → 生成简历
- **绝不是**「粘贴一大段 → 切成 N 个岗位 → 批量建 N 个任务 → N 份简历」（这是被用户明确否掉的旧模型）
- 页面：`01 资料 / 02 工作区（默认）/ 03 简历`。没有「岗位」页、没有「任务」看板。

## 资料的两层结构
- **全局资料库**（`Profile` 表）= 主资料，在「资料」页维护
- **per-chat 快照**（`Chat.profile_snapshot`，整体快照非增量 diff）= 新建 chat 时深拷贝带入
  - chat 内可改快照，**不影响全局、不影响其他 chat**
  - `POST /api/chats/{id}/profile/reset` 重置回全局
  - 理由：diff 合并语义反直觉；chat 是冻结的工作上下文

## 覆盖清单（Coverage）—— 核心产品能力
简历**必须覆盖岗位每一条要求**，同时产出覆盖清单，逐条标依据：
- `grounded`：来自资料真实经历，**必须有 `evidence_ref`**
- `capability`：资料无直接对应，仅「具备…能力/意向」表述
- **严禁编造可证伪的具体事实**：公司名、年限、项目名、数字、学历、证书
- 存在 `Resume.coverage`（JSON）；前端在「简历工作台 → 匹配分析」tab 渲染，徽章区分两档
- 岗位解析要把 `Employer questions`（申请表提问）**单独拎出来，不进入简历正文**，前端做成可折叠卡片

## 技术栈（已定，不要随意更换）
| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | FastAPI + SQLModel + SQLite(WAL) | 零外部依赖，开箱即跑 |
| 并发 | asyncio.Queue + N worker 协程 | 与 FastAPI 同事件循环；升级路径 → Celery/Redis |
| LLM | OpenAI 兼容 `/chat/completions`（httpx 异步） | 可指向 DeepSeek / 通义 / OpenAI |
| 前端 | 原生 ES Module，无构建工具 | ~1500 行体量，不值得上 Vite |
| 设计 | Ark UI `ark` 家族 + `moderate` 深度 | 近黑底 / 青色 #18d1ff / 草绿 #c8eb21 / 方角 |
| PDF | 浏览器打印（矢量文字） | 免重依赖；服务端批量可后补 Playwright |

## 核心架构约束（改动时必须遵守）
1. **`Chat` 是工作单元**（由旧 `JobTask` 重命名演进而来）。**不要新建并行的表**——
   调度器全以 chat id 为键（asyncio.Queue + 条件 UPDATE 抢占 + 重启恢复 + 指数退避），
   重命名是机械替换、能复用全部已验证的并发逻辑；新建表等于重写调度器，风险过高。
2. **模型输出的是 resume spec JSON，不是 HTML。** HTML 由 `renderer.py` 确定性渲染。
   这是 XSS 防线，也是「零 LLM 成本重排版」的基础。
3. **HTML 是简历唯一真相来源**，`Resume.spec` 只是生成时的结构快照。
4. **`Resume.chat_id` 是单向关联**（已删除 `Chat.resume_id`）。旧的双向引用是
   「删除要先解绑外键」那套绕路代码的根源。删除 chat 级联删 Resume + EditLog + ChatMessage。
5. **重新生成 = 覆盖**（一 chat 一简历），但记进 `EditLog(instruction="重新生成")` 以便回退。
6. **DB 是任务队列的真相来源**，`asyncio.Queue` 只是触发器。必须保留 `scheduler._recover()`。
7. 无 API Key 时必须走 `mock.py` 降级链路，且顶栏明示「降级模式」。

## 兼容层（有意保留，不要删）
`/api/tasks*` 标了 `deprecated=True`，内部映射到 Chat。
它让 5 个 HTTP 级测试（smoke / regression / xss_probe / progress_probe / e2e_verify）
继续充当「调度器 / 重试 / 取消 / 渲染 / 净化」的安全网 —— 这些恰是重构**没有**动的部分。
等新链路彻底稳定后再考虑下线。

## LLM 配置（供应商预设）
- `app/providers.py` 是**供应商预设的唯一真相来源**：10 个供应商，**DeepSeek 排第一且为默认**（`DEFAULT_PROVIDER_ID`）。
- 前端设置弹窗**不让用户填 Base URL**：选供应商 → 自动填 URL 与模型 → 只填 API Key。
- `POST /api/settings/llm/test` 做「测试连接」：先打 `GET {base_url}/models`（**该端点需鉴权**，200=有效/401=无效），
  404/405 时回退发一次最小 `/chat/completions` 探测。返回真实模型列表供下拉框刷新。
- **DeepSeek 事实（已核实，勿凭印象改）**：Base URL `https://api.deepseek.com`；
  模型 `deepseek-flash` / `deepseek-v4-pro`（旧名 `deepseek-v4-flash` 等已退役，**不要写进预设**）；
  Key 申请 `https://platform.deepseek.com/api_keys`。
- 非 DeepSeek 的预设模型名可能随版本变化，UI 已标注「建议测试连接拉取真实列表」，并提供「＋ 自定义模型名」兜底。
- **`PUT /api/settings/llm` 不带 provider_id 时必须按 base_url 反查**，否则会留下与 base_url 不一致的陈旧 provider_id
  （根因：`set_runtime_config` 跳过值为 None 的键）。

## 已知技术坑（勿重复踩）
- FastAPI 204 路由不能有响应体 → 用 `response_class=Response` + `return Response(status_code=204)`
- `Content-Disposition` 中文文件名 → 必须 RFC 5987 `filename*=UTF-8''<urlquote>`
- 简历 CSS 有全局选择器 → 必须用 iframe 隔离，不能 contenteditable 主文档
- **禁止手写正则做 HTML 净化** → 用 `nh3`（`nh3==0.3.7`）。正则永远堵不完，
  已实测被 `<img/onerror=...>`（`/` 分隔）、`<svg/onload=...>`、`&#x61;`/`&#106;` 实体编码绕过
- `nh3` 的 `clean_content_tags` 只能放 `script`，放 `style` 会 panic
- `nh3` 的 removed 标志不能用 `clean != html` 判断（会被规范化误报），要用独立探测器

## 安全架构（两层，改动时别破坏）
1. **入库净化**：`app/sanitize.py` 用 nh3 白名单净化 **AI 编辑产物**（`ai_edit.py` 写库前）。
   只净化 AI 路径，不净化用户手动保存（`PUT /api/resumes/{id}`）——用户自伤不是威胁模型。
2. **出口 CSP**：`renderer.build_preview_document()` 与前端 `printResume()` 都注入
   `default-src 'none'; style-src 'unsafe-inline'; img-src data: https:`。
   这层**不依赖净化器正确性**，即使净化被绕过脚本也执行不了。**不要加 `frame-ancestors`**（meta CSP 不支持，会产生控制台警告）。

## 关键语义约定
- **一个 chat = 一个求职场景 = 恰好一份简历**：`DELETE /api/chats/{id}` 级联删
  Resume + EditLog + ChatMessage（运行中返回 400）。
  `DELETE /api/resumes/{id}` 也会**同时删除关联 chat**（保持「单元」语义一致）。
- **非法 layout/theme 返回 400（不是 422）**：400 带可读中文提示，前端 toast 友好；
  刻意不用 Pydantic Literal（422 的 detail 是结构化列表，弹到 toast 里是 JSON 串）。
- 所有把 chat `status` 改写为 `queued`/`failed` 的地方**必须同时 `progress = 0`**，
  且 WebSocket 广播**必须带 `progress` 字段**（前端是合并 payload 的）。
- `pipeline.run_chat` 通过注入 `should_cancel` 回调检查取消，**不要改成 import scheduler**（循环导入）。
- **WebSocket 路径仍是 `/ws/tasks`**（历史命名），但**事件名是 `chat_message` / `chat_progress` / `chat_updated`**，
  payload 带 `chat_id`。

## `llm_max_tokens` 必须够大
`SYSTEM_AI_EDIT` 要模型返回**整份简历 HTML**（约 9KB），4096 token 会被截断
→ 表现为「无法解析 JSON」这种误导性报错。**当前约定 `max_tokens <= 0` = 不限制**（请求体完全不发送该字段，由服务端决定），默认值 0；`llm.py` 仍保留 `finish_reason == "length"` 检测抛可读错误。设置页可调。
**温度同理不发送**：请求体不含 temperature，由各家服务端默认值决定（用户明确不要暴露这两个参数）。
**worker 数不暴露**：前端设置弹窗只有供应商/Base URL/Key/模型/max_tokens 五项。

## API Key 已配置状态（前端必须醒目展示）
设置弹窗有 `#cfg-key-status` 状态条：已配置显示「✓ API Key 已配置（脱敏）」，未配置显示降级模式提示。
`POST /api/settings/llm/test` 未填 key 时**回落到已存储 key**（仅同供应商；custom 需 base_url 一致），
用户无需重新输入即可测试连接。

## 本机环境坑（重要）
**本机设了 `http_proxy`，代理会拦截 localhost 请求并返回 502。**
- curl 加 `--noproxy '*'`；httpx 用 `trust_env=False` 或设 `NO_PROXY=localhost,127.0.0.1`
- 起服务做本地验证时，**给服务进程也带上 `NO_PROXY`**，否则应用自身的本地请求会被代理拦掉
- 应用侧已内置处理：`providers.is_local_url()` 命中本机地址时 `trust_env=False`，外部地址仍走代理
- Playwright `launch()` 首跑偶发被杀，加 `--no-sandbox --disable-gpu --no-proxy-server` 后稳定

## 验证方式
```bash
.venv/Scripts/python.exe start.py                      # 一键启动（推荐）
.venv/Scripts/python.exe backend/tests/smoke.py        # 后端全链路（TestClient）
.venv/Scripts/python.exe backend/tests/regression.py   # 缺陷回归（209 项，TestClient）
.venv/Scripts/python.exe backend/tests/ui_check.py     # 前端走查（需服务在跑）
.venv/Scripts/python.exe backend/tests/chat_e2e.py     # 聊天式工作区端到端（需服务，BASE_URL 可指定）
.venv/Scripts/python.exe backend/tests/xss_probe.py    # XSS 端到端（自起隔离 DB+端口）
.venv/Scripts/python.exe backend/tests/e2e_verify.py   # 浏览器层+安全边界（需服务在跑）
.venv/Scripts/python.exe backend/tests/progress_probe.py  # 进度归零采样
.venv/Scripts/python.exe backend/tests/settings_ui_check.py  # 模型配置前端实测
```
Playwright 用 `channel="msedge"` 复用系统 Edge，**不要 `playwright install chromium`**。

### ⚠️ 跑测试一定要用隔离库
进程内测试（`smoke` / `regression` / `progress_probe`）默认打**共享 `data/app.db`**，
而开发服务器也在用同一个文件 —— 会**争抢写入导致偶发失败**，还会把测试数据写进用户库。
```bash
DATABASE_URL="sqlite:///./data/test.db" python backend/tests/regression.py
```
`backend/tests/_dbguard.py` 的 `warn_if_shared_db()` 会在用共享库时打印醒目警告（不阻断运行）。
**新增进程内测试脚本时记得调它。**

**注意执行顺序**：测试脚本若要在 `TestClient(app)` 之前访问 DB，必须**先调 `init_db()`** ——
建表是 TestClient 的 lifespan 触发的，顺序反了在干净库上会 `no such table`。

## ⚠️ 用户库里配了真实 DeepSeek API Key
`data/app.db` 的 `llm_api_key` 是**真实可用**的 key。因此：
- **跑测试必须用隔离 DB**（`DATABASE_URL=sqlite:///./data/xxx.db` 并剥掉 `llm_*` 设置）走降级链路
- **不要往共享 `data/app.db` 写测试数据**，也不要大量调用真实 LLM 消耗用户额度
- 需要验证真实 LLM 路径时，最多做一两次调用并说明

## 用户偏好
- 设计倾向 Ark UI（已自行安装该 skill），布局随意
- 中文交流
