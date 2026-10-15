# 测试体系（10 个套件）

本项目**不使用 pytest**，而是 10 个可直接运行的独立脚本，放在 `backend/tests/`。
每个脚本自己打印 `PASS/FAIL` 明细并以**退出码**表达结论（`0` = 全绿，非 `0` = 有失败），
方便人工走查，也方便接进 CI。

所有需要联网 localhost 的运行都要加 `NO_PROXY=localhost,127.0.0.1`
（本机设置了 `http_proxy`，否则 localhost 请求会被代理拦截）。

---

## 一、套件总览表

| 套件（`backend/tests/`） | 类型 | 端口 | 数据库 | 覆盖点 |
|---|---|---|---|---|
| `smoke.py` | 进程内（TestClient） | 无需端口 | `data/_smoke.db`（自动隔离） | 主链路冒烟：资料→批量建任务→并发生成→取简历→AI 编辑→改版式→导出/预览→设置 |
| `regression.py` | 进程内（TestClient） | 无需端口 | `data/_regression.db`（自动隔离） | 已确认缺陷回归 + 契约：P0-1~P2-13、P3-3、P4-1~P4-4、F1~F4 |
| `progress_probe.py` | 进程内（TestClient） | 无需端口 | `data/_progress_probe.db`（自动隔离） | 进度重置：重排队（queued）与最终失败（failed）时 `progress` 归零 |
| `xss_probe.py` | 浏览器（headless Edge） | **8123** | `data/_ui_xss_probe.db` | XSS 双层防御：ai-edit 入库净化（nh3）+ CSP 兜底 |
| `e2e_verify.py` | 浏览器（headless Edge） | **8122** | `data/_ui_e2e_verify.db` | 净化器无损/中和、CSP、5 种版式的浏览器布局几何、关键 API 契约 |
| `chat_e2e.py` | 浏览器（headless Edge） | **8125** | `data/_ui_chat_e2e.db` | 聊天式工作区端到端：三视图→新建→贴 JD→生成→消息流/雇主问题→覆盖清单→设置 |
| `ui_check.py` | 浏览器（headless Edge） | **8121** | `data/_ui_ui_check.db` | 全流程走查 + 逐步截图；**强制 0 控制台 error** |
| `settings_ui_check.py` | 浏览器（headless Edge） | **8124** | `data/settings_ui.db` | 设置弹窗：供应商预设 / Base URL / 模型下拉 / 测试连接各分支 / 保存回显 / Key 脱敏 |
| `iv_check.py` | 浏览器（headless Edge） | **8126** | `data/_ui_iv_check.db` | AI 问答（flashcard）：资料页入口、工作区入口（含 gaps）、跳过路径、浅色/移动端 |
| `ux_check.py` | 浏览器（headless Edge） | **8127** | `data/_ui_ux_check.db` | UX 优化项：命令面板、快捷键、职位名识别、粘贴简历导入、**工作区粘贴简历**、**命令面板粘贴 URL 回车即抓取** |

> 「类型」说明：**进程内** = `fastapi.testclient.TestClient(app)` 直接跑 ASGI 应用，不起网络端口；
> **浏览器** = 起一个真实 uvicorn 子进程，再用 Playwright 驱动系统 Edge（`channel="msedge"`）访问真实页面。
>
> **端口分配原则**：8121~8127 各套件独占，互不冲突；`8000` 是用户开发服务端口，测试脚本**绝不触碰**。

---

## 二、怎么跑

通用形式（Windows / Git Bash）：

```bash
# 进程内套件：不设 BASE_URL / DATABASE_URL，脚本自动用隔离库
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/smoke.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/regression.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/progress_probe.py

# 浏览器套件：不设 BASE_URL，脚本会自起隔离服务（自动选端口）
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/ux_check.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/iv_check.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/ui_check.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/chat_e2e.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/e2e_verify.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/xss_probe.py
NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/settings_ui_check.py
```

**默认隔离**：两类脚本都遵循「不显式指定就自动隔离」（见 `isolation.md`）：

- 进程内套件：不设 `DATABASE_URL` → 自动指向 `data/_<脚本名>.db`；
- 浏览器套件：不设 `BASE_URL` → 通过 `_ui_server.spawn_isolated_server()` 自起一个隔离库子进程。

只有显式设置 `BASE_URL`（打外部服务）或 `DATABASE_URL` 才会改变行为，且脚本会打印醒目提示。
**打 8000 端口会把测试数据写进用户真实库 `data/app.db`，不要这么做。**

### 浏览器套件的前置条件

- 本机需装 **Microsoft Edge**（用 `channel="msedge"`，无需 Playwright 再下载 Chromium）。
- Playwright 启动参数固定为 `headless=True, args=["--no-sandbox", "--disable-gpu", "--no-proxy-server"]`。
- 若环境禁止 Playwright 自行拉起浏览器，`settings_ui_check.py` 支持 `PLAYWRIGHT_CDP_URL`
  连接外部已启动的 Edge（见该脚本 docstring）。

---

## 三、逐个套件说明（覆盖什么 / 通过判据）

### 1. `smoke.py` —— 端到端冒烟

- **覆盖**：保存资料 → `POST /api/tasks/batch` 建 2 条任务 → 轮询到全部 `succeeded`
  → 取简历 HTML → AI 二次编辑（mock 链路）→ `POST /api/resumes/{id}/layout` 改版式
  → `/download` 与 `/preview` → 读取 LLM 设置。
- **通过判据**：每步 `assert` 通过，最后打印 `全部通过 ✅`，退出码 `0`。

### 2. `regression.py` —— 缺陷回归 + 契约

- **覆盖**：两大批已确认缺陷（P0-1~P1-8、P0-9~P2-13）与后续项：
  - P1-7 非法 layout/theme → 400 且不落库；P0-4 标题 XSS 转义；P1-8 删简历；
  - P1-5 五种版式 HTML 哈希两两不同；P1-6 未知 section type 兜底；
  - P0-1 删任务外键/级联；P0-2 取消运行中任务；P1-M4 retry 守卫；P2-11 retry 返回体；
  - P0-3 运行时调 worker 数不卡 running；P3-3 本地地址绕过代理；
  - **P4-x**：Chat 资料快照 / 覆盖语义 / 覆盖清单 / 级联删除 / 409 / 迁移脚本；max_tokens 语义；
  - **F1** URL 导入（mock 抓取层，含 file:// 拒绝、抓取失败、无 JD 分支）；
  - **F2** AI 问答（资料页 + 工作区，只增补不覆盖）；
  - **F3** `/api/profile/import-text`（覆盖写库语义）；
  - **F4** `/api/chats/{id}/profile/import-text`（工作区粘贴简历：正常解析 / 只增补不覆盖 / 空文本 400 / 快照为空直接采用）。
- **通过判据**：`_FAIL == 0`，打印 `全部通过 ✅`，退出码 `0`。未通过时逐条列出 `FAIL`。

### 3. `progress_probe.py` —— 进度重置探针

- **覆盖**：清空资料制造「稳定失败」任务，每 0.2s 采样 `status/progress/attempts/stage`。
- **通过判据**：全程未出现 `queued 且 progress != 0`，且最终 `status == "failed"` 且 `progress == 0`
  → 打印 `PROBE PASS ✅`。结束时会把资料恢复回原值。

### 4. `xss_probe.py` —— XSS 双层防御

- **覆盖**：自起隔离服务（8123）。A) `ai-edit` 路径：6 条已知绕过 payload 经净化后入库，
  再让无头 Edge 加载 `/preview`；B) 直接用 `PUT` 写入原始 payload（模拟净化被绕过），验证 CSP 兜底。
- **通过判据**：存储内容不含 `onerror` / `<svg` / `javascript:`；`/preview` **无 alert**、
  标题未被脚本改写、简历仍正常渲染（`.resume` 高度 > 200、`.r-name` font-weight=800）；
  CSP 头含 `default-src 'none'`。全通过退出码 `0`。

### 5. `e2e_verify.py` —— 浏览器层 + 安全边界

- **覆盖**：与 `regression.py` 的分工是「只有真浏览器才能验的部分」：
  净化器中和已知绕过 + 正常片段无损；`/preview` 的 CSP 头；
  AI 编辑注入后「入库无恶意 → 浏览器不执行且 CSP 违规可观测」；
  干净简历在 CSP 下样式完整（`.r-name` font-weight=800、`.r-body` 仍是 grid、`@page` 可解析）；
  5 种版式的实际布局几何两两不同；关键 API 契约（非法 layout/theme→400、非法 scope→422、
  `/print` 已移除→404、删任务 cascade 语义）。
- **通过判据**：`BAD` 为空，退出码 `0`。

### 6. `chat_e2e.py` —— 聊天式工作区端到端

- **覆盖**：加载应用 + 三视图导航 → 新建对话（快照栏显示姓名）→ 填职位名 + 贴 JD（四类分节）
  → 生成（含生成前问答，出现即「跳过」）→ 消息流 JD 气泡 + 雇主问题折叠卡 → 打开简历
  → 匹配分析里的覆盖清单渲染 → 设置弹窗含「最大输出 token」。
- **通过判据**：`BAD` 为空 + 控制台 0 error/warning，退出码 `0`。

### 7. `ui_check.py` —— 全流程走查（截图）

- **覆盖**：从「01 资料」逐字段填写 → 新建对话 → 贴 JD → 生成 → 打开简历 → 切各编辑 tab
  （源码 / AI / 版式 / 分析）→ AI 修改 → 设置 → 移动端。逐步落盘截图到 `backend/tests/shots/`。
- **通过判据**：**控制台 0 条 error**（`errors` 为空）。为规避 headless Edge 退出期偶发挂起，
  脚本在收尾处显式 `os._exit(code)` 并**先**停掉隔离服务（见 `isolation.md`）。

### 8. `settings_ui_check.py` —— 设置弹窗实测

- **覆盖**：供应商下拉（DeepSeek 默认第一并选中、Base URL 只读自动填充、切换供应商/custom 可编辑）、
  模型下拉（含预设与「＋ 自定义模型名…」）、测试连接各分支（空 Key / 404 / 不可达 → 可读中文提示，
  且**不真的打外部网络**）、保存后顶栏与页脚回显、重开弹窗的 Key 状态条与脱敏回填。
- **通过判据**：`_FAIL == 0` 且 `_LOGS`（控制台 error）为空，退出码 `0`。

### 9. `iv_check.py` —— AI 问答（flashcard）

- **覆盖**：资料页入口（出卡→填卡→来回切卡不丢答案→提交→`GET /api/profile` 核对回写）；
  工作区入口（贴 JD→生成→出卡含 gaps→填部分→确认→生成态→succeeded）；
  「跳过，直接生成」路径；浅色模式弹窗可读；375px 不溢出。
- **通过判据**：`BAD` 为空 + 控制台 0 error/warning，退出码 `0`。

### 10. `ux_check.py` —— UX 优化项

- **覆盖**：
  1. `Ctrl+K` 命令面板（打开 / 过滤 / ↓+Enter 执行「打开 简历」并切视图 / Esc 关闭）；
  2. 粘贴含「Senior Frontend Engineer」的 JD → 职位名自动回填；
  3. `Ctrl+Enter` 在岗位要求框触发生成；
  4. 生成成功后主按钮变「查看简历 →」、编辑字段后恢复「重新生成」；
  5. 资料页「粘贴简历文本导入」→ 弹窗 → 解析 → 表单回填；
  6. 命令面板在深色/浅色/375px 下可读、无横向溢出；
  7. **工作区粘贴简历原文** → 弹确认（含「只增补，不覆盖」）→ 确认 → 快照写入经历/技能；
     以及「不覆盖」（先设姓名再导入，姓名不被改写）与「粘贴 JD 不误触发」；
  8. **命令面板粘贴 URL** → 出现「回车即抓取该链接」提示 + 只留「导入该链接」→ 回车复用导入流程；
     以及「第一次 Esc 退出 URL 模式、第二次才关面板」；
  9. 全程 **0 控制台 error/warning**。
- **通过判据**：`BAD` 为空（且控制台 0 error/warning），退出码 `0`。

---

## 四、产物

- 截图：`backend/tests/shots/`（已被 `.gitignore` 忽略）。文件名见各脚本内的 `page.screenshot(...)`，
  例如 `ux-01-command-palette-dark.png`、`ux-07-paste-resume-confirm.png`、`ux-08-cmd-url-mode.png`。
- 隔离数据库：`data/_<script>.db`（进程内测试）、`data/_ui_<name>.db`（UI 测试自起服务）、
  `data/settings_ui.db`（settings_ui_check 自己的隔离库）（运行结束时多数会被清掉）。
