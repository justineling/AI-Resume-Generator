# 功能：资料库（`01 资料`）

> 这篇讲「资料库」里到底有哪些字段、三种录入方式各自适合什么场景、以及「全局资料」和「对话快照」的关系。
> 适合：准备填资料的使用者；要改资料表单/导入逻辑的开发者。

资料库是整个系统的**素材来源**：模型只会从你填写的内容里取材，不会凭空编造学历、公司与数字。页面在 `01 资料`，顶栏有两个辅助入口：「让 AI 帮我完善」「粘贴简历文本导入」。

对应实现：`backend/app/models.py:Profile`、`backend/app/schemas.py:ProfileIn`、`backend/app/static/js/profile.js`、`backend/app/routers/profile.py`。

---

## 1. 字段结构

资料在库里是**全局单份**（`Profile` 表，首次访问自动创建空档案）。字段分两部分。

### 1.1 标量字段（8 个）+ `extra` 字典

> 严格说标量是 **8 个**（`name`/`headline`/`email`/`phone`/`location`/`age`/`avatar`/`summary`，即 `pipeline.py` 的 `_PROFILE_SCALAR`）；
> 另有 1 个 `extra`（JSON dict，用于存放未建模的附加信息），它不是标量，故合计 9 个**界面字段**。
> 下表的 9 行中，最后一行 `extra` 属字典类型。

| 字段 | 界面控件 | 说明 |
|---|---|---|
| `name` 姓名 | `#pf-name` | 生成简历时的姓名；为空会被视作「空资料」 |
| `headline` 一句话定位 | `#pf-headline` | 如「5 年后端工程师」 |
| `email` 邮箱 | `#pf-email` | |
| `phone` 电话 | `#pf-phone` | |
| `location` 所在城市 | `#pf-location` | |
| `age` 年龄 | `#pf-age`（number） | 可空；输入框为空时存 `null` |
| `avatar` 头像 | 上传头像 `#pf-avatar-file` / 清除 `#btn-avatar-clear` | 存为 data-url，内嵌进简历 HTML，导出不依赖外部文件；单张限 2MB |
| `summary` 个人简介 | `#pf-summary`（textarea） | 2-3 句核心竞争力 |
| `extra` | 无界面 | 承接无法归类但值得保留的零散信息（如导入时产生的其它字段），字典结构 |

### 1.2 卡片列表（8 类）

界面表单里可增删的重复条目，对应页面上的 PF-02 ~ PF-07 各面板。

| 卡片 | 前端键名 | 子字段 |
|---|---|---|
| 链接 | `links` | `label`（名称）、`url`（地址） |
| 工作经历 | `experiences` | `company`、`title`、`location`、`start`、`end`、`summary`、`achievements[]`（每行一条）、`tech[]`（逗号分隔） |
| 项目经历 | `projects` | `name`、`role`、`start`、`end`、`link`、`summary`、`highlights[]`（每行一条） |
| 专业技能 | `skills` | `name`、`level`（精通/熟练/了解）、`category`（语言/框架/工具）；另有一个**不由表单直接编辑**的 `note`（技能深度线索，由「AI 问答」的回答整合写入） |
| 教育背景 | `education` | `school`、`degree`、`major`、`start`、`end`、`highlights[]` |
| 证书 | `certificates` | `name`、`issuer`、`date` |
| 荣誉 | `awards` | `name`、`date`、`detail` |
| 语言 | `languages` | `name`、`level` |

> 结构约定的权威来源是 `backend/app/schemas.py` 顶部注释（前端与 LLM 共享同一份）。

底部操作栏：`#btn-save-profile` 保存资料、`#btn-export-profile` 导出 JSON、`导入 JSON`（`#pf-import-file`，选中文件后**只回填表单**，需再点「保存资料」）。

---

## 2. 三种录入方式对比

| 方式 | 入口 | 适合场景 | 消耗 | 产出 | 是否写库 |
|---|---|---|---|---|---|
| ① 手工填写 | 资料页各表单 → 「保存资料」 | 从零开始、字段不多、想要完全可控 | 无（不发请求） | 你逐字填的内容 | 点保存后 `PUT /api/profile` 写库（整体覆盖） |
| ② 让 AI 帮我完善（问答） | 资料页 `#btn-profile-ai` | 已有一版资料，但「不够具体 / 没有量化」想被追问补强 | 一次问答调用（有 Key 用 LLM，无 Key 走启发式） | flashcard 问题；你回答后被**增补**进全局资料 | 提交后 `POST /api/profile/interview/apply` 写库（只增补） |
| ③ 粘贴简历文本导入 | 资料页 `#btn-profile-import-text` | 已有一份现成简历（PDF/Word 里的纯文本），想一次性灌进来 | 一次解析调用（LLM 优先，无 Key 走正则启发式） | 结构化资料，回填整个表单 | `POST /api/profile/import-text` 写库（**整体覆盖**，见下） |

> 工作区里还有一个**只对本对话生效**的「粘贴简历原文」入口，语义是「只增补不覆盖」，见 [chat-workspace.md](./chat-workspace.md#新功能工作区直接粘贴简历原文)。

---

## 3. 「粘贴简历文本导入」流程（资料页 = 整体覆盖）

1. 点右上角「粘贴简历文本导入」→ 弹出 `#resume-import-dialog`（标题「粘贴简历文本导入」）。
2. 弹窗提示明确写着：**「注意：这会覆盖当前资料表单内容。」**
3. 把简历全文（纯文本即可）粘贴进 `#ri-text` → 点「解析并填入」（`#btn-import-text-run`，期间文案变「解析中…」）。
4. 后端 `POST /api/profile/import-text`：
   - `app/profile_import.py:parse_profile_text()` **LLM 优先**（提示词 `SYSTEM_PARSE_RESUME`，硬约束「只提取文本真实存在的信息，绝不编造」）；
   - 无 API Key / 解析失败 / 结果为空 → 回落 `mock.parse_resume_text()`（纯正则 + 分块，绝不抛错）；
   - 两条路径的结果都先经 `normalize_profile()` 收敛成 `ProfileIn` 形状（字段类型安全、字符串有长度上限、列表元素永远是 dict）。
5. 返回的完整资料**直接写库**（同 `PUT /api/profile` 语义）：`data` 里所有字段都会 `setattr` 到 `Profile` 行上，因此这是一个**覆盖式**写入，不是合并。
6. 前端 `applyProfileData(saved)` 回填整页表单，toast 提示「已从简历文本导入 N 段经历 / M 项技能」。

### 3.1 「只增补不覆盖」语义存在于哪里

`app/profile_import.py` 里另有一组**合并函数**，供**工作区**的导入使用（资料页不用）：

| 函数 | 语义 |
|---|---|
| `merge_profiles(base, incoming)` | 只增补：标量字段「base 已有非空值就保留，为空才用 incoming 填」；列表字段按「身份键」去重后追加；`extra` 按键只填空缺 |
| `merge_summary(before, after)` | 统计本次各列表新增了多少条（用于给用户可读反馈） |

列表去重用的「身份键」（`_IDENTITY`）：

| 列表 | 身份键 |
|---|---|
| `links` | `url` + `label` |
| `experiences` | `company` + `title` |
| `projects` | `name` |
| `skills` | `name` |
| `education` | `school` + `degree` + `major` |
| `certificates` / `awards` / `languages` | `name` |

> 小结：**资料页导入 = 覆盖**（`/api/profile/import-text`）；**工作区导入 = 只增补**（`/api/chats/{id}/profile/import-text`，调用 `merge_profiles`）。这一点容易记混，两处交互在界面上也给了不同提示。

---

## 4. 全局资料 vs 对话快照

| | 全局资料（`Profile` 表） | 对话快照（`Chat.profile_snapshot`） |
|---|---|---|
| 范围 | 全局唯一一份 | 每个 chat 一份 |
| 何时产生 | 在资料页保存 | 新建 chat 时对全局资料做一次**整体深拷贝**（`create_chat` / `import-url` 都会做） |
| 谁在用 | 资料页 | 生成简历时**只读快照**（`pipeline._profile_from_snapshot`），不读全局 |
| 改动影响面 | 影响此后新建的对话 | 只影响本对话，**不影响全局、也不影响其它对话** |
| 恢复 | 无（直接编辑） | 「恢复默认」`#btn-chat-profile-reset` → 用当前全局资料重新快照 |

为什么这么设计：一个对话 = 一个求职场景。你可能针对某个岗位想微调措辞/突出某段经历，但不想污染总档案、也不想影响别的岗位。快照把「这个场景用的资料」冻结下来，让生成结果可复现。

### `profile_dirty` 是什么

`Chat.profile_dirty`（布尔）表示**本对话的快照是否被单独改动过**：

- 新建对话时 `False`；
- 在对话里编辑快照（`PUT /api/chats/{id}/profile`）、提交 AI 问答补充、或工作区粘贴导入 → 置 `True`；
- 点「恢复默认」重新快照 → 置回 `False`。

界面上，工作区资料条 `#ws-profile-badge` 在 `profile_dirty=true` 时显示「已单独调整」，提示你这份对话用的资料已经和总档案不同了。

对话内可编辑的资料：`#btn-chat-profile-edit` 打开 `#chat-profile-dialog`，只暴露**姓名 / 一句话定位 / 个人简介**三个身份字段（保存时以服务端快照为基底，只覆盖这三个字段，经历/项目/技能等其它素材保留）。

---

## 常见陷阱

1. **资料页「粘贴简历文本导入」会覆盖整个表单**：它会清掉你当前手填/之前问答补的内容。想保留已有内容，请改用工作区里的「粘贴简历原文」（只增补），或先手填后再导入前自行确认。弹窗里已有醒目提示。
2. **保存按钮不是自动的**：`导入 JSON` 只是把数据回填到表单，**必须再点「保存资料」**才会写入库（toast 也会提醒「已导入，记得点保存」）。
3. **改完资料不会自动更新已存在的对话**：快照是「新建对话那一刻」的拷贝。改完全局资料后，旧对话仍用旧快照，需要在该对话点「恢复默认」才会同步（且会丢掉该对话的单独调整）。
4. **`name` 为空可能被判为空资料**：生成时若「姓名、经历、技能、教育」全为空，会被判定为「空资料」并拒绝生成（提示「尚未填写资料库…」）。至少要有姓名，或一段经历/技能/教育。
5. **头像走 data-url**：上传后内嵌为 data-url，简历 HTML 因此自包含、可离线导出；但也意味着头像越大 HTML 越大，界面已限制单张 2MB。
6. **`skills.note` 不在表单里**：它由「AI 问答」的回答整合（增量里的 `skill_notes`）写入，且只更新**已存在**的技能名；生成阶段还会在简历 spec 里另外生成 note（不写回资料库）。手填表单不会产生 `note`，这不影响使用。
