# 功能：招聘链接导入（URL → 抓取 → 填入新对话）

> 这篇讲「从链接导入」怎么用、抓取是怎么实现的（为什么必须带完整浏览器头）、抽取逻辑与已知限制。
> 适合：使用链接导入的使用者（尤其遇到抓不到内容时）；要改抓取/抽取逻辑的开发者。

实现：`backend/app/static/js/chat.js`（导入面板交互）、`backend/app/routers/chats.py:import_chat_from_url`（接口）、`backend/app/fetch.py`（抓取 + 抽取）、提示词 `SYSTEM_EXTRACT_URL`（`backend/app/prompts.py`）。

---

## 1. 使用流程

入口有三个（都指向同一个导入面板）：

- 工作区左侧栏「从链接导入」`#btn-import-url`；
- 工作区空态里的「从链接导入」`#btn-empty-import-url`；
- 命令面板里 `Ctrl+K` → 粘贴 URL → 回车（见 [command-palette.md](./command-palette.md#新功能在命令面板里粘贴-url回车即抓取)）。

步骤：

1. 点「从链接导入」→ 展开导入面板 `#import-url-box`（标题「从招聘链接导入」）。面板提示：「粘贴职位详情页链接，系统会抓取页面并识别**职位名称**与**岗位要求**，填入一个新对话。仅支持 http(s)，抓取超时 20 秒。」
2. 在 `#import-url-input` 粘贴职位详情页链接。
3. 可选：勾选「导入后自动生成简历」`#import-auto-generate`。
4. 点「开始导入」（`#btn-import-confirm`，期间按钮变「抓取中…」，状态区 `#import-url-status` 显示「正在抓取网页并识别职位信息…」）。
5. 前端调用 `POST /api/chats/import-url` → 成功后自动打开新建的对话：
   - 抓到岗位要求 → toast「已从链接导入职位信息，请核对无误后点「生成简历」」；
   - 勾了自动生成 → toast「已导入职位信息，正在准备生成前的问题…」并直接进入生成流程（**与手动点「生成简历」完全同一条路径**，因此仍会先做生成前问答）；
   - 未识别到岗位要求 → toast「已创建对话，但未能识别岗位要求，请手动补充」。

> 注意：无论是否勾选自动生成，前端都向后端传 `auto_generate=false`，生成一律交给 `generateChat()` 走完整流程（先问答再生成），避免绕过问答步骤。

### 截图位置

自动化用例产出的相关截图在 `backend/tests/shots/`：

| 截图 | 内容 |
|---|---|
| `ux-08-cmd-url-mode.png` | 命令面板的「回车即抓取该链接」URL 模式（`ux_check.py`） |
| `03-chat-new.png` / `04-chat-jd.png` / `05-chat-done.png` | 工作区新建对话 / 填入 JD / 生成完成（`ui_check.py`） |
| `ux-04-profile-import.png` | 资料页「粘贴简历文本导入」弹窗（`ux_check.py`） |

> 导入面板本身暂无专门的自动化截图；它出现在工作区点「从链接导入」之后。

---

## 2. 抓取实现（`app/fetch.py`）

`fetch_url_text(url)` 的流程：

1. `validate_url()` 校验并规范化（见 §3）。
2. 用 `httpx.AsyncClient` 发起 `GET`：
   - **超时 20 秒**（`FETCH_TIMEOUT = 20.0`）；
   - `follow_redirects=True`；
   - **本机地址直连、外部地址走系统代理**：`trust_env=not is_local_url(url)`；
   - 请求头为完整的浏览器头 `BROWSER_HEADERS`，并补一个**同源 `Referer`**（`scheme://netloc/`）。
3. 状态码 ≥ 400 → 抛 `FetchError`「网页返回了错误状态（HTTP xxx），请确认链接可直接在浏览器打开」。
4. `Content-Type` 不含 `html` / `text` → 抛 `FetchError`「该链接不是网页（可能是 PDF / 图片），请粘贴招聘详情页的网址」。
5. 响应体截断到 **2MB**（`MAX_HTML_BYTES = 2_000_000`）；用正则取 `<title>`；用**标准库 `HTMLParser`**（不引第三方解析器）抽可见正文，跳过 `script/style/nav/footer` 等区域，压缩空白。
6. **不执行页面 JS**（静态抓取，这是硬限制）。
7. 抽到的正文少于 80 字符 → 抛 `FetchError`「未能从该页面提取到有效内容——该网站可能由脚本动态渲染，请改为手动复制职位名称与岗位要求」。
8. 返回 `{final_url, page_title, text}`，其中 `text` 截断到 **20000 字符**（`MAX_TEXT_CHARS`）。

### 为什么必须带完整浏览器头

`BROWSER_HEADERS` 里除了 `User-Agent`，还有 `Accept`、`Accept-Language`、`Accept-Encoding`、`sec-ch-ua`、`sec-ch-ua-mobile`、`sec-ch-ua-platform`、`sec-fetch-dest`、`sec-fetch-mode`、`sec-fetch-site`、`sec-fetch-user`、`upgrade-insecure-requests`、`cache-control`，并在请求时补同源 `Referer`。

这是**踩过的坑**：实测只带 `User-Agent` 会被 JobStreet 这类站点判为爬虫、直接返回 **403**；补齐上述头（尤其 `sec-fetch-*` 与同源 `Referer`）后实测返回 **200**。所以这些头不是「可有可无的礼貌」，而是能否抓到内容的关键。

---

## 3. `validate_url` 的协议校验顺序

`validate_url` 的关键点是**先显式拒绝非 http(s) 协议，再补前缀**：

1. 去空白；为空 → 报错「请填写招聘页面的链接」。
2. 用正则判断是否已带协议（`^[a-zA-Z][a-zA-Z0-9+.-]*://`）。
   - 若带协议、且不是 `http(s)://` → **直接拒绝**：「仅支持 http(s) 网页链接」。
3. 若**没有**协议 → 补 `https://`。
4. 再用 `urlparse` 校验 scheme 属于 `http/https` 且存在 hostname，否则报「链接格式不正确…」。

> 为什么顺序很重要：如果先补前缀再校验，`file://x` 会被盲加成 `https://file://x` 从而**绕过协议白名单**。先判「是否自带协议」并只对无协议输入补前缀，才能挡住 `file://`、`ftp://` 等。

---

## 4. 职位信息抽取：LLM 优先，启发式回落

`extract_jd_from_text(text, client)`：

1. **LLM 优先**（已配置 API Key 时）：用 `SYSTEM_EXTRACT_URL` 提示词，要求返回 `{job_title, requirements}`：
   - 找出准确职位名（页面上只有一个职位就以它为准；整页是职位列表则挑与正文篇幅最匹配的主职位）；
   - 抽取岗位要求**原文**（按行整理，不翻译、不改写、不总结），排除申请表问题、薪资勾选项、导航与广告；
   - 找不到有效信息时两个字段返回空串。
2. LLM 不可用 / 失败 / 未返回 `requirements` → 回落 `_heuristic_extract`：
   - 在前 15 行里找最像标题的短行作为职位名（排除小节标题、列表项）；
   - 定位 `Requirements/任职要求/岗位要求` 等小节，截取到下一个已知小节标题为止；
   - 找不到要求小节就取前 60 行兜底。

后端在写库前再判一次：`requirements` 为空 → 返回 **422**「已抓取网页，但未能识别出岗位要求。该页面可能是职位列表或由脚本渲染，请打开链接手动复制职位名称与岗位要求。」

---

## 5. 已知限制

| 限制 | 说明 / 应对 |
|---|---|
| **不执行页面 JS** | 纯前端渲染（SPA）的招聘页可能抓不到正文 → 提示改为手动复制职位名与岗位要求 |
| 只抓单个页面 | 只 `GET` 目标 URL（跟随跳转），不会点进列表里的子职位 |
| 非网页内容 | PDF / 图片链接会被内容类型检查拦下 |
| 需要登录的页面 | 静态抓取无 Cookie，登录墙页面抓不到正文 |
| 反爬 | 已用完整浏览器头应对常见站点；仍可能有更严格的反爬返回 403 |
| 正文截断 | 送入抽取的正文上限 20000 字符；超长页面可能丢失末尾内容 |

---

## 常见陷阱

1. **403**：说明目标站点仍把请求当爬虫。可先在浏览器里复制好职位名与要求手动粘贴；若你是开发者，检查请求头是否被改动（删掉 `sec-fetch-*` / `Referer` 会退化回 403）。
2. **抓到了「职位列表页」**：页面含多个职位时，抽取可能识别不出唯一职位 → 返回 422 或要求不准确。请改用**职位详情页**的链接。
3. **抽取不到要求**：动态渲染页面常见。提示里已给出出路——手动复制职位名称与岗位要求，直接贴进对话的「岗位要求」框。
4. **不要把 `file://`、`ftp://` 之类粘进来**：会被直接拒绝（「仅支持 http(s) 网页链接」）。裸域名（如 `example.com/job/1`）会自动补 `https://`。
5. **导入后别急着等结果**：导入只负责「填好一个新对话」，真正的生成在对话里（勾了自动生成则会进入生成前问答，仍需你回答或跳过）。
