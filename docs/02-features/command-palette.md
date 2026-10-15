# 功能：命令面板与快捷键（`Ctrl/Cmd + K`）

> 这篇讲命令面板能做什么、怎么用键盘操作、有哪些全局快捷键、以及无障碍是怎么实现的。
> 适合：想用键盘提效的使用者；要扩展命令或改快捷键的开发者。

实现：命令注册与快捷键都在 `backend/app/static/js/app.js`（`baseCommands` / `resumeCommands` / `renderCommands` / `initPalette` / `initShortcuts`）；DOM 在 `backend/app/static/index.html`（`#command-palette`）。

---

## 1. 打开与操作

| 操作 | 按键 |
|---|---|
| 打开 / 关闭面板 | `Ctrl + K`（macOS 为 `Cmd + K`） |
| 上下移动高亮 | `↑` / `↓`（或在选项上移动鼠标） |
| 执行高亮命令 | `Enter` |
| 关闭面板 | `Esc` |

打开后面板会把**焦点自动移入输入框**（`#cmd-input`），关闭时把焦点**还原**到打开前的元素。

---

## 2. 命令分组

面板按分组呈现（`renderCommands` 在每个分组前插入 `role="presentation"` 的分组标题）。

| 分组 | 命令 | 行为 |
|---|---|---|
| 工作区 | 新建对话 | 等价于点「＋ 新建」 |
| 工作区 | 从链接导入 | 切到工作区并打开「从链接导入」面板 |
| 导航 | 打开 资料 | 切到 `01 资料` |
| 导航 | 打开 工作区 | 切到 `02 工作区` |
| 导航 | 打开 简历 | 切到 `03 简历` |
| 系统 | 设置（模型配置） | 打开设置弹窗 |
| 系统 | 切换颜色模式：深色 / 浅色 / 跟随系统 | 立即切换主题 |
| 系统 | 刷新数据 | 重新拉取对话与简历 |
| 简历 | 打开简历：<标题> · `RESUME-xxxx · vN` | **动态生成**：取最近 5 份简历（`resumeCommands`），点击直接打开该简历 |

> 「简历」组是异步拉取的：面板先以基础命令渲染，拿到简历列表后再补充（`buildPaletteCommands`）。

---

## 3. 模糊匹配

在输入框里打字即时过滤（`input` 事件 → `renderCommands`）。匹配规则是**子序列模糊匹配**（`fuzzyMatch`）：查询串的每个字符按顺序出现在命令标题里即命中，空格忽略。

- 例：输入「打开」→ 保留三条「打开 资料 / 工作区 / 简历」；
- 无匹配时显示空态 `#cmd-empty`（「没有匹配的命令」）。

---

## 4. 无障碍实现

| 要求 | 实现 |
|---|---|
| 弹层语义 | `#command-palette` 是原生 `<dialog>`，带 `aria-label="命令面板"`，用 `showModal()` 打开 |
| 组合框语义 | 输入框 `#cmd-input` 为 `role="combobox"`，`aria-expanded="true"`，`aria-controls="cmd-list"`，`aria-autocomplete="list"` |
| 列表语义 | `#cmd-list` 为 `role="listbox"`；每个命令按钮为 `role="option"` |
| 选中态 | 高亮项设 `aria-selected="true"`（其余 `false`），随 ↑↓ / 鼠标移动更新 |
| 活动项 | 输入框维护 `aria-activedescendant="cmd-opt-N"`，随高亮同步；无结果时移除 |
| 焦点管理 | 打开时焦点入框；关闭时还原到打开前的元素（且校验该元素仍在文档中） |
| 键盘消费 | `↑↓`/`Enter` 在面板内 `preventDefault` + `stopPropagation`，不冒泡成全局快捷键 |
| 输入法安全 | 键盘处理先判 `ev.isComposing`（中文输入法组合中不触发） |

---

## 5. 全局快捷键表

`initShortcuts()` 在 `document` 上监听 `keydown`（`meta = ctrlKey || metaKey`）：

| 快捷键 | 生效范围 | 行为 |
|---|---|---|
| `Ctrl/Cmd + K` | 全局 | 打开 / 关闭命令面板（切换） |
| `Ctrl/Cmd + S` | 全局 | **始终 `preventDefault`**（阻止浏览器「保存网页」）；在 `01 资料` 页等价于点「保存资料」；在 `03 简历` 页由 `resume.js` 单独监听并保存简历（`app.js` 不再重复处理） |
| `Ctrl/Cmd + Enter` | 上下文主操作 | flashcard 弹窗打开时 → 点「确认提交」；在 `01 资料` 页 → 点「保存资料」；在 `02 工作区` 的职位名 / 岗位要求输入框上按 → 触发「生成简历」（由 `chat.js` 处理，避免重复触发） |
| `Esc` | 上下文 | 关闭当前弹窗（含 flashcard 的「取消」语义、命令面板、设置等） |

**中文输入法守卫**：处理 `Ctrl+S` / `Ctrl+Enter` 时先判 `isComposing(ev)`（`ev.isComposing || ev.keyCode === 229`）；命令面板与工作区输入框的键盘处理同样有 `ev.isComposing` 守卫。因此**用中文输入法组合字符时不会误触发快捷键**。

---

## 【新功能】在命令面板里粘贴 URL，回车即抓取

**目的**：把「打开工作区 → 点从链接导入 → 粘贴 URL → 点开始导入」压缩成「Ctrl+K → 粘贴 → 回车」。

**怎么触发**：按 `Ctrl/Cmd + K` 打开面板后，**直接把链接粘贴到输入框**（`#cmd-input` 的 `paste` 事件）。

**判定**：`looksLikeUrl(text)` —— 无空白字符，且是 `http(s)://…` **或** 形如 `example.com/path` 的裸域名。

**进入「URL 模式」**（`enterUrlMode`）后：

- 输入框内容变成该 URL；
- 出现提示行 `#cmd-url-hint`：「回车即抓取该链接」，并把链接显示在 `#cmd-url-value`；
- 命令列表**只剩一条**：分组「招聘链接」、命令「导入该链接」（副标题为该 URL）；此时打字**不会**触发普通模糊搜索（`input` 事件在 URL 模式下直接返回）。

**回车执行**（`Enter` → `runUrlImport`）：

1. 关闭命令面板；
2. 复用既有「从链接导入」流程（`performUrlImport`）：切到工作区 → 点 `#btn-import-url` 打开导入面板 → 把 URL 填入 `#import-url-input` → 点 `#btn-import-confirm` 触发抓取。

> 也就是**不另写一套抓取逻辑**，而是复用 `/api/chats/import-url` 与导入面板（详见 [url-import.md](./url-import.md)）。抓取成功后会新建一个已填好职位名与岗位要求的对话。

**Esc 的两级行为**：

- 在 URL 模式下按第一次 `Esc` → **只退出 URL 模式**（清空提示与输入、回到普通命令列表），**面板仍然打开**；
- 再按一次 `Esc` → 关闭面板。

---

## 常见陷阱

1. **`Ctrl+S` 必须 `preventDefault`**：否则会弹出浏览器的「保存网页」。这也是为什么代码在全局监听里**先**阻止默认行为、再按视图决定保存什么。若你在别处新增 `Ctrl+S` 处理，注意不要漏掉这一步。
2. **`Ctrl+S` 在简历页保存的是简历**（由 `resume.js` 处理），不是资料；资料页保存的是资料。别在简历页指望它保存资料。
3. **命令面板的 `Ctrl+Enter` 不用于主操作**：面板里用 `Enter` 执行高亮命令；`Ctrl+Enter` 是全局上下文主操作（见上表）。
4. **中文输入法组合期间快捷键失效是刻意设计**：看到「按了没反应」先确认输入法是否在组合态（`isComposing`），这是为了避免误触发。
5. **URL 模式会吞掉输入**：进入后普通命令被隐藏、打字不生效——按一次 `Esc` 退回普通模式即可。
6. **命令列表随视图变化**：切换视图后重新打开面板，命令集与「最近简历」会刷新。
