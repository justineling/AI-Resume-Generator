# 功能：简历工作台（`03 简历`）

> 这篇讲简历工作台的 5 个编辑 tab、编辑与保存、AI 二次修改与回退、版式与主题、匹配分析里的覆盖清单，以及导出。
> 适合：要改简历、换版式、导出 PDF 的使用者；要改简历编辑/渲染逻辑的开发者。

实现：`backend/app/static/js/resume.js`（工作台交互）、`backend/app/routers/resumes.py`（查询/保存/重排/预览/下载）、`backend/app/routers/ai_edit.py`（AI 修改与回退）、`backend/app/renderer.py`（spec → 自包含 HTML）。

---

## 1. 顶部工具栏

| 元素 | id | 说明 |
|---|---|---|
| 标题 / 元信息 | `#rs-title` / `#rs-meta` | 元信息形如 `RESUME-0001 · v3 · ark · 时间` |
| tab 切换 | `.tab[data-tab=…]` | 见 §2 |
| 保存 | `#btn-save-resume` | 有未保存修改时才可用（`disabled` 表示已同步） |
| 打印 / PDF | `#btn-print` | 见 §5 |
| 下载 HTML | `#btn-download` | 见 §5 |
| 删除 | `#btn-delete-resume` | 会**连带删除关联对话**（一对一单元） |

左侧「简历列表」`#resume-list` 列出所有简历（标题、`#id · v版本 · 主题 · 时间`），点击即打开。

---

## 2. 五个编辑 tab

| tab | `data-tab` | 做什么 |
|---|---|---|
| 所见即所得 | `wysiwyg` | 在 `iframe#editor-frame` 里直接改文字与顺序（`designMode=on`）。修改后 `#edit-state` 从「已同步」变「有未保存修改」 |
| HTML 源码 | `source` | 直接在 `#source-area` 编辑 HTML；点「应用到编辑器」`#btn-apply-source` 写回编辑器并标记为未保存。保存后与所见即所得视图同步 |
| AI 修改 | `ai` | 用自然语言让模型改 HTML（见 §4），对话记录在 `#ai-chat` |
| 版式 | `layout` | 手动覆盖版式与主题（见 §3） |
| 匹配分析 | `analysis` | 覆盖清单 + 岗位解析结果（见 §6） |

> 「所见即所得」是默认 tab。切到「HTML 源码」时会先把编辑器当前内容同步到源码框，保证看到的是最新的。

### 编辑与保存

- 编辑器里的任何输入/按键改动都会把 `dirty` 置真并启用「保存」。
- 点「保存」（或按 `Ctrl/Cmd + S`）→ `PUT /api/resumes/{id}`（只发 `html`）→ 版本号 +1，toast「已保存（vN）」，`#edit-state` 回到「已同步」，左侧列表刷新。
- HTML 是**唯一真相来源**：保存后源码框与预览都用最新的 `html`。

---

## 3. 版式与主题

「版式」tab 提示：「版式由模型按岗位特征决定。你也可以在这里手动覆盖，系统会基于结构快照重新渲染，**不消耗 LLM 调用**。」

### 5 种布局

| 布局 id | 名称 | 副标题 | 预览图形 |
|---|---|---|---|
| `single-column` | 单栏经典 | ONE COLUMN | 1 条满宽 |
| `sidebar-left` | 左主右辅 | MAIN + ASIDE | 2 条：约 72% + 24% |
| `two-column` | 左侧栏 | ASIDE + MAIN | 2 条：约 24% + 72% |
| `timeline` | 时间轴 | TIMELINE | 3 条满宽 |
| `compact-grid` | 紧凑网格 | COMPACT | 2 条：约 48% + 48% |

**预览图形怎么读**：每个卡片里的 `<i>` 元素就是一条「栏」，其宽度（`flex-basis` 百分比）代表该布局的**列宽比例**——两条不同宽度的竖条表示「主栏 + 侧栏」，同样宽度表示「等宽网格」；时间轴用三条满宽表示纵向堆叠。**第一条**竖条带 `sig`（信号色）高亮，只是视觉锚点。点某个卡片即选中，点「应用版式」`#btn-apply-layout` 生效。

> 真实差异由渲染器的 CSS 驱动：`sidebar-left` / `two-column` 是「主栏 + 236px 侧栏」两列网格（靠顺序交换左右），`timeline` 给每条经历加时间轴节点，`compact-grid` 把内容排成两列网格。

### 4 种主题配色

`ark`（默认）、`endfield`、`exa`、`corporate`。在「版式」tab 下以关键词标签形式选择（当前选中为 `kw-hit`）。主题决定简历里的信号色与侧栏底色，**只影响简历 HTML 本身**，与应用的深浅色模式互不影响（见「常见陷阱」）。

- `ark`：默认（青色信号）
- `endfield`：信号色偏黄绿（`#8a7a00`）
- `exa`：信号色偏青（`#0f8f83`）
- `corporate`：信号色偏橄榄（`#3d4a00`）

点「应用版式」→ `POST /api/resumes/{id}/layout`（`{layout, theme}`）→ 后端基于已保存的 **spec 快照**重新渲染 HTML（不调用 LLM），版本号 +1。

---

## 4. AI 修改与回退

「AI 修改」tab：

| 元素 | id | 说明 |
|---|---|---|
| 指令输入 | `#ai-instruction` | 如「把工作经历里的第三条改得更量化」「把技能章节移到最前面」「整体语气更简洁」 |
| 范围 | `#ai-scope` | `whole`（整份简历）/ `section`（指定章节） |
| 目标 | `#ai-target` | 选「指定章节」时填 section id（可选） |
| 执行修改 | `#btn-ai-run` | 期间变「修改中…」 |
| 回退上一版 | `#btn-ai-undo` | 见下 |

- 执行 → `POST /api/resumes/{id}/ai-edit`（`{instruction, scope, target, current_html}`，`current_html` 优先于库内未保存内容）→ 返回 `{ok, html, summary}`。
- 成功：编辑器与源码框同步成新 HTML，版本 +1，聊天区插入 AI 的改动说明；**模型返回的 HTML 入库前会先净化**（剥离脚本 / 事件处理器 / 危险协议，防存储型 XSS）。
- 失败：插入错误气泡并保持原内容；失败也会记一条 `ok=false` 的编辑记录。
- **每次编辑都写一条 `EditLog`**（指令、范围、成功与否、前后 HTML）。聊天区下方会显示「历史」：最近 N 次编辑，形如 `✓ 指令 | ✗ 指令`。
- 「回退上一版」（`#btn-ai-undo`）→ `POST /api/resumes/{id}/undo`：把 HTML 还原成**最近一次成功编辑的 `before_html`**，版本 +1；没有可回退记录时返回 400「没有可回退的编辑记录」。

> 生成阶段的「再次生成」也会记一条 `instruction="重新生成"` 的 `EditLog`，所以刚重新生成过也可以用「回退上一版」救回旧内容。

---

## 5. 导出

| 操作 | 实现 |
|---|---|
| 打印 / PDF | 新开窗口写入简历 HTML（带 CSP 限制），约 400ms 后自动唤起打印，选「另存为 PDF」即导出**矢量文字**的 PDF。若弹窗被拦截：先保存，再退化为打开后端独立打印视图 `/api/resumes/{id}/preview`（在新标签页手动 `Ctrl+P`） |
| 下载 HTML | 下载为独立的 `.html` 文件（自带内联样式，可离线打开/打印）。下载前若有未保存修改会**先保存**，保证下载的是最新版 |

下载文件名按简历标题生成（含中文时用 RFC 5987 编码，并提供 ASCII 回退名 `resume.html`）。

> 简历 HTML 是**自包含**的（内联 `<style>`，头像为 data-url），所以打印和下载都不依赖外部文件。

---

## 6. 匹配分析

「匹配分析」tab（`#analysis-body`）按顺序展示：

1. **覆盖清单 / COVERAGE**（`renderCoverage`，见下）；
2. 若岗位原文超长被截断：提示「岗位原文超过 20000 字符，已截断后解析，匹配分析可能未覆盖全部内容。」；
3. **版式与取舍说明 / MATCH NOTES**（生成时给出的排版说明）；
4. **雇主可能问到的问题 / EMPLOYER QUESTIONS**（申请表元数据，不进简历正文）；
5. **岗位分节 / SECTIONS**（Res/Requirements/… 的分节原文）；
6. **岗位关键词 / KEYWORDS**（命中你技能的用 `kw-hit` 高亮）；
7. 硬性要求 / 主要职责 / 加分项 / 软技能；
8. 已忽略的噪音 / DROPPED；
9. 技术栈 / STACK。

### 覆盖清单的两档徽章

覆盖清单把「岗位要求」逐条映射到「简历依据」，每条带一个来源徽章：

| 徽章 | `source` | 含义 | 附带信息 |
|---|---|---|---|
| **有据**（`is-grounded`） | `grounded` | 该要求在简历里**有真实依据**（来自你的资料） | 必须有 `evidence_ref`（出处），界面显示「出处：experiences[0].achievements[0]」等；通常还有「简历依据：工作经历·ACME：…」 |
| **能力**（`is-capability`） | `capability` | 资料中**无直接对应**，只按「能力层面」表述 | 无出处；常见说明「资料中无直接对应经历，按能力层面表述」 |

顶部统计（`cov-stats`）：**总条目 / 有据可依 / 能力层面 / 未覆盖**。

> 硬规则：`grounded` 必须带 `evidence_ref`，否则后端会把它降级为 `capability`（「无依据不得标 grounded」）。若某条`requirements` 完全没有覆盖项，后端会补一条 `capability` 兜底（计入 `uncovered`），保证不漏条。
>
> 换句话说：**「有据」越多，说明简历越扎实**；「能力」多，说明你的资料对这份 JD 覆盖不足——可以去补一段经历，或回到工作区用「生成前问答」补齐。

---

## 常见陷阱

1. **简历预览在 iframe 里隔离**：编辑器用 `srcdoc` 把简历 HTML 加载进 `iframe` 并开启 `designMode`。这是为了**隔离样式**（简历自带 `ark/endfield/…` 主题与内联 CSS，不能被应用界面样式污染）并做所见即所得。因此你不能用页面的 CSS 去改简历样式——要改就改源码 tab 或换主题。
2. **简历不跟随应用的深浅主题**：应用顶栏的深色/浅色（`arg.theme`）只改界面；简历颜色由它自己的 `data-theme`（`ark/endfield/exa/corporate`）决定。想让简历变深色需要在源码里加 `data-tone="dark"`（降级模式的「切换深色」指令就是这么做的），或选一个深色观感的主题。
3. **`HTML 源码` 改了要点「应用到编辑器」**：直接切 tab 不会自动应用你的源码修改。
4. **打印被浏览器拦截弹窗**：会退化为打开后端预览页；在新标签页按 `Ctrl+P` 即可。
5. **「指定章节」需要填 section id**：范围选 `section` 但不填 `#ai-target` 时，后端按「整份」处理（提示里会说明本次范围）。section id 可在源码里看到（如 `data-section-id="skills"`）。
6. **AI 修改依赖 LLM**：无 API Key 的降级模式只支持有限指令（如「删除技能章节」「切换深色」），其它指令会原样返回并给出提示。
