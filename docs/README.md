# AI Resume Generator · 文档总索引

> 这篇是整套文档的**入口页**。想快速搞清项目怎么跑、每个功能怎么用、代码在哪，从这里出发。
> 适合：第一次接触本项目的开发者、使用者、评审者。

**一句话介绍**：一个本地运行的「岗位定制简历生成器」——粘贴岗位要求（JD），用大模型分析岗位并改写你的素材，产出一份针对性简历，再在浏览器里所见即所得地编辑、导出 PDF。数据全部留在本机。

**核心理念：一个 chat（对话）= 一个求职场景 = 恰好一份简历。**

- 投不同岗位时，你不需要维护很多份简历；每个「对话」就是一个独立场景：贴一份 JD、自带一份该场景专用的资料快照、只产出这一份简历。
- 「对话」同时是后台调度的最小单位（排队 / 并发 / 重试 / 取消都按它来）。
- 「资料」页是**全局素材库**，新建对话时会对它做一次快照；在对话里改资料只影响该对话，不影响全局，也不影响其它对话。

---

## 文档地图

| 文档 | 讲什么 | 谁写的 |
|---|---|---|
| [01-architecture/overview.md](./01-architecture/overview.md) | 系统总览：分层、启动生命周期、事件流 | 架构组 |
| [01-architecture/data-model.md](./01-architecture/data-model.md) | 数据模型：Profile / Chat / ChatMessage / Resume / EditLog / AppSetting，关系与级联删除 | 架构组 |
| [01-architecture/pipeline.md](./01-architecture/pipeline.md) | 生成流水线：阶段划分、取消机制（`should_cancel` 回调） | 架构组 |
| [01-architecture/scheduler.md](./01-architecture/scheduler.md) | 调度与并发：队列、worker、退避重试、状态机不变式 | 架构组 |
| [01-architecture/security.md](./01-architecture/security.md) | 安全：转义 / 净化 / XSS 防护面 | 架构组 |
| [02-features/profile-library.md](./02-features/profile-library.md) | 资料库：字段结构、三种录入方式、全局资料 vs 对话快照 | 产品（本文档集） |
| [02-features/chat-workspace.md](./02-features/chat-workspace.md) | 聊天式工作区：界面元素、生成流程、状态机、取消/重试/删除 | 产品 |
| [02-features/ai-interview.md](./02-features/ai-interview.md) | AI 问答（flashcard）：生成前补齐资料缺口、增量整合契约 | 产品 |
| [02-features/url-import.md](./02-features/url-import.md) | 招聘链接导入：抓取实现、浏览器头、抽取与已知限制 | 产品 |
| [02-features/resume-studio.md](./02-features/resume-studio.md) | 简历工作台：5 个编辑 tab、版式与主题、覆盖清单、导出 | 产品 |
| [02-features/command-palette.md](./02-features/command-palette.md) | 命令面板与快捷键：命令分组、无障碍、快捷键表 | 产品 |
| [03-api/reference.md](./03-api/reference.md) | 后端 API 参考：所有路由、请求/响应、错误约定 | 架构组 |
| [04-frontend/modules.md](./04-frontend/modules.md) | 前端模块划分与交互实现（原生 ES Module，无构建工具） | 前端组 |
| [04-frontend/design-system.md](./04-frontend/design-system.md) | 前端设计系统：颜色 token、组件、深浅色模式 | 前端组 |
| [05-testing/suite.md](./05-testing/suite.md) | 测试套件：regression / chat_e2e / ux_check 等如何跑、覆盖什么 | 测试组 |
| [05-testing/isolation.md](./05-testing/isolation.md) | 测试数据隔离：隔离库防污染真实数据 | 测试组 |
| [06-operations/getting-started.md](./06-operations/getting-started.md) | 启动与配置：三种启动方式、首次配置、数据与备份、故障排查 | 产品（本文档集） |
| [06-operations/llm-providers.md](./06-operations/llm-providers.md) | LLM 供应商：10 个预设、配置步骤、测试连接、降级模式 | 产品 |
| [06-operations/desktop-installer.md](./06-operations/desktop-installer.md) | 桌面端安装包：Tauri 2 + Python sidecar、构建流程、默认装 D 盘、数据与日志位置、启动排障 | 运维 |

> `01-architecture/`、`03-api/`、`04-frontend/`、`05-testing/` 由对应同事维护，本页只做导航，不复述其内容。
> 旧设计稿已归档到 [`99-history/`](./99-history/)（`system_design.md` + 类图/时序图），它是「聊天式工作区重构」的历史方案，部分内容已被后续实现超越；当前系统以本索引下的文档为准。

---

## 我想做 X → 看哪篇

| 我想…… | 看这篇 |
|---|---|
| 把项目跑起来 / 换端口 / 排查服务起不来 | [06-operations/getting-started.md](./06-operations/getting-started.md) |
| 配置大模型（DeepSeek / OpenAI / Ollama…）或没有 API Key 也能用 | [06-operations/llm-providers.md](./06-operations/llm-providers.md) |
| 打一个 exe 安装包给没有 Python 的人用 / 桌面端启动白屏 / 安装目录跑偏 | [06-operations/desktop-installer.md](./06-operations/desktop-installer.md) |
| 搞懂怎么填「资料」，或直接粘贴一份现成简历文本导入 | [02-features/profile-library.md](./02-features/profile-library.md) |
| 生成一份简历（整体流程、按钮状态、取消/重试） | [02-features/chat-workspace.md](./02-features/chat-workspace.md) |
| 生成前被问了一堆问题，想知道为什么、Esc 和「跳过」有什么差别 | [02-features/ai-interview.md](./02-features/ai-interview.md) |
| 粘贴招聘页链接却抓取失败（403 / 抓不到内容） | [02-features/url-import.md](./02-features/url-import.md) |
| 改简历、换版式、看「覆盖清单」两档徽章、导出 PDF | [02-features/resume-studio.md](./02-features/resume-studio.md) |
| 用键盘操作 / Ctrl+K 命令面板 / 快捷键按了没反应 | [02-features/command-palette.md](./02-features/command-palette.md) |
| 加一个 API、想知道路由与错误约定 | [03-api/reference.md](./03-api/reference.md)（架构组） |
| 加一个前端视图 / 改交互 / 改设计 token | [04-frontend/modules.md](./04-frontend/modules.md)、[04-frontend/design-system.md](./04-frontend/design-system.md)（前端组） |
| 跑测试 / 改数据模型后确认没回归 / 避免污染真实库 | [05-testing/suite.md](./05-testing/suite.md)、[05-testing/isolation.md](./05-testing/isolation.md)（测试组） |
| 理解「为什么一个对话只产一份简历」「资料快照是什么」 | [01-architecture/data-model.md](./01-architecture/data-model.md) + [02-features/chat-workspace.md](./02-features/chat-workspace.md) |

---

## 30 秒快速上手

```bash
# 1) 装依赖（首次 1-2 分钟）——二选一
python start.py                 # 一键：建 venv → 装依赖 → 检查端口 → 起服务 → 开浏览器

# 2) 打开浏览器（start.py 会自动打开；也可手动访问）
#    http://127.0.0.1:8000

# 3) 配一个模型（可选；不配也能用降级模式）
#    顶栏「设置」→ 选「DeepSeek 深度求索」→ 填 API Key → 点「测试连接」→ 选模型 → 保存设置
```

最小可用流程（约 1 分钟）：

1. 左侧导航 `01 资料` → 填「姓名」+ 至少一段「工作经历」或几项「专业技能」→ 点「保存资料」。
2. 左侧导航 `02 工作区` → 点「＋ 新建」→ 在右侧「岗位要求」里粘贴一段 JD。
3. 点「生成简历」。若弹出「生成前补充资料」的卡片问答，可回答、也可直接点「跳过，直接生成」。
4. 右侧会出现进度条与阶段；生成完成后按钮变为「查看简历 →」，点它进入 `03 简历`。
5. 在简历工作台调整（版式 / AI 修改），最后点「打印 / PDF」导出。

> 更细的每一步说明见 [02-features/](./02-features/) 下各篇；
> 没有 API Key 时，系统走本地启发式「降级模式」，流程完全一致，只是内容质量下降、AI 问答与修改能力受限。

---

## 项目目录速览

```
AI Resume Generator/
├── start.py / start.bat / start.sh     # 三种一键启动方式（start.py 为核心逻辑）
├── requirements.txt / .env.example     # 依赖与配置模板
├── backend/
│   ├── run.py                          # 直接跑 uvicorn（不走 start.py 时用）
│   └── app/
│       ├── main.py                     # FastAPI 入口：路由挂载、WebSocket、静态资源、启动生命周期
│       ├── config.py                   # 配置（.env 可覆盖）；数据目录 data/
│       ├── db.py                       # 引擎 / 会话；SQLite + WAL
│       ├── migrate.py                  # 旧模型迁移（jobtask → chat 等）
│       ├── models.py                   # 数据表：Profile / Chat / ChatMessage / Resume / EditLog / AppSetting
│       ├── schemas.py                  # 请求响应模型 + 素材结构约定（前端与 LLM 共享）
│       ├── scheduler.py                # asyncio 队列 + 多 worker + 退避重试 + 取消/重启恢复
│       ├── events.py                   # WebSocket 事件广播（chat_message / chat_progress / chat_updated）
│       ├── pipeline.py                 # 单个 chat 的生成流水线（解析 → 生成 → 渲染 → 落库）
│       ├── interview.py                # AI 问答（flashcard）：生成问题 + 增量整合回答
│       ├── fetch.py                    # 招聘链接抓取 + 职位信息抽取
│       ├── profile_import.py           # 粘贴简历文本 → 结构化资料（含只增补合并）
│       ├── renderer.py                 # spec → 自包含 HTML（Jinja2 + 内联样式）
│       ├── sanitize.py                 # HTML 净化（防存储型 XSS）
│       ├── llm.py / providers.py       # LLM 客户端 / 供应商预设
│       ├── prompts.py / mock.py        # 提示词模板 / 无 Key 时的启发式实现
│       ├── routers/                    # profile / chats / tasks(兼容) / resumes / ai_edit / settings
│       └── static/                     # 前端：index.html + css/app.css + js/*.js（原生 ES Module，无构建）
├── backend/tests/                      # regression / chat_e2e / ux_check / ui_check / smoke ...（另见 05-testing/）
├── data/app.db                         # SQLite 数据库（WAL 模式）；备份建议见 getting-started
└── docs/                               # 本套文档
```

---

## 关键约定速查

| 主题 | 约定 |
|---|---|
| 版本与端口 | Python ≥ 3.11；默认 `127.0.0.1:8000`，被别的程序占用时**默认自动顺延**并醒目告知实际端口（`--strict-port` 才改为报错）；就绪前用 `/api/health` 的 `app` 标识确认「确实是本应用」才开浏览器 |
| 状态机 | `draft → queued → running → succeeded / failed / cancelled` |
| 进度阶段 | 10% 载入资料快照 · 25% 解析岗位要求 · 60% 规划版式并生成内容 · 85% 渲染简历版式 · 100% 完成 |
| 覆盖语义 | 一个 chat 恰好一份简历；再次生成为「删旧建新」并留一条可回退的编辑记录 |
| 降级模式 | 未配置 API Key 时走 `app/mock.py` 启发式，功能链路完整、不调用大模型 |
| 本机代理 | 访问 localhost 必须绕过系统代理：`NO_PROXY=localhost,127.0.0.1`，curl 加 `--noproxy '*'` |
