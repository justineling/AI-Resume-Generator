# 运维：启动与配置

> 这篇讲怎么把项目跑起来（三种启动方式各自做什么）、首次要配置什么、数据存在哪、遇到问题怎么排查。
> 适合：第一次运行的使用者、部署/排障的开发者。

相关文件：`start.py`（一键启动核心逻辑）、`start.bat`、`start.sh`、`requirements.txt`、`.env.example`、`backend/app/config.py`。

> **想给没有 Python 的人用？** 本文讲的是 web 端（本机跑 `start.py`、浏览器访问）。
> 如果要产出一个双击即用的 Windows 安装包，看 [`desktop-installer.md`](./desktop-installer.md)。
> 两种形态**数据相互独立**：web 端用 `<项目根>/data/app.db`，桌面端用 `%LOCALAPPDATA%\AIResumeGenerator\data`。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | **≥ 3.11**（`start.py` 里 `MIN_PY = (3, 11)`；创建虚拟环境前会校验版本） |
| 依赖 | 见 `requirements.txt`：`fastapi==0.115.6`、`uvicorn[standard]==0.34.0`、`sqlmodel==0.0.22`、`httpx==0.28.1`、`jinja2==3.1.5`、`python-multipart==0.0.20`、`pydantic-settings==2.7.1`、`websockets==14.1`、`nh3==0.3.7` |
| 网络 | 首次需联网装依赖；不配大模型也能用（降级模式） |
| 浏览器 | 现代浏览器（前端用到原生 ES Module、`<dialog>`、`designMode`） |

---

## 2. 三种启动方式

| 方式 | 命令 | 作用 |
|---|---|---|
| 一键脚本 | `python start.py` | 核心逻辑：建虚拟环境 → 装依赖 → 检查端口 → 起服务 → 开浏览器 |
| Windows 双击 | `start.bat` | 找到 `py -3` 或 `python` 后调用 `start.py`；找不到 Python 会给出安装提示并暂停；退出码非 0 时暂停以便看报错 |
| macOS / Linux | `./start.sh`（首次 `chmod +x start.sh`） | 找到 `python3` 或 `python` 后 `exec python ./start.py "$@"` |

### `start.py` 到底做了什么

1. **准备虚拟环境** `ensure_venv()`：若 `.venv` 不存在则创建；若当前解释器不是 `.venv` 里的，则用 `.venv` 的解释器**重新执行本脚本**（用 `subprocess` 传参数列表而非 `os.execv`，避免 Windows 上含空格路径被拆坏）。用环境变量 `_RESUME_GENERATOR_REEXEC` 防止无限重执行。
2. **安装 / 更新依赖** `ensure_deps()`：读 `requirements.txt` 算 sha256，写入 `.venv/.deps-stamp`；**只有依赖文件变了才重装**。装完会 `import` 关键包（fastapi/uvicorn/sqlmodel/httpx/jinja2/nh3）验证真的可用，失败则给出提示。
3. **选端口**：先探测目标端口（**IPv4 与 IPv6 回环双栈探测**：`127.0.0.1` 与 `[::1]` 任一被监听即视为占用）；若**已被本应用**占用 → 直接打开浏览器、不重复起服务；若被**别的程序**占用 → **默认自动顺延**到下一个空闲端口并**醒目告知**实际地址（`--strict-port` 时才改为明确报错、指出占用者并退出码 1）。
4. **起服务**：`uvicorn app.main:app --app-dir backend --host <host> --port <port>`；`--reload` 时追加开发模式。
5. **等健康检查后开浏览器**：后台线程轮询 `/api/health`，**解析 JSON 校验 `app == "ai-resume-generator"` 且 `ok == true`** 才视为就绪，就绪后打开 `http://<host>:<port>`；若子进程在启动期退出（bind 失败 / 端口竞态）立即判定失败、不等满超时，且**失败分支绝不打开浏览器**。

**常用参数**：

```bash
python start.py --port 9000      # 指定端口（被别的程序占用时会自动顺延）
python start.py --strict-port    # 端口被占用时直接报错退出（默认是自动顺延）
python start.py --host 0.0.0.0   # 改监听地址（默认 127.0.0.1）
python start.py --no-browser     # 不自动打开浏览器
python start.py --reload         # 开发模式：改代码自动重启
python start.py --reinstall      # 强制重装依赖
```

### 端口被占用时怎么办

**默认行为：自动顺延到下一个空闲端口，并醒目告知实际地址。**
端口被别的程序占用时，启动器会自动顺延（打印 `[注意] X 被占用，已顺延到 Y；本服务不在 X 上！`
以及「将打开 `http://<host>:Y`」），开箱即用。
之所以**必须醒目告知**而不是静默顺延：一旦悄悄改到别的端口，而客户端、书签或文档仍以原端口为契约，
你按旧地址打开时连到的就会是**占用该端口的那个程序**，而不是本应用——这正是「打开别人的页面」事故的根源。

**想改为「被占用即报错」（严格模式）：**

```bash
python start.py --strict-port   # 端口被占用时直接报错退出，不自动顺延
```

严格模式会打印占用者（进程名 / PID / 命令行，来自 `netstat` / `tasklist` / PowerShell，定位不到时给出手动排查命令），
并给出出路（换端口 / 结束占用进程）。

**也可直接指定端口：**

```bash
python start.py --port 9000     # 指定端口（被占用时同样自动顺延）
start.bat --port 9000           # Windows 双击场景
```

> `--auto-port` 仍可用，但已是**默认行为**，保留仅为兼容旧脚本/习惯。

**启动成功前会校验「确实是本应用」**：启动器请求 `/api/health`，解析 JSON 校验稳定标识
`app == "ai-resume-generator"` 且 `ok == true`。只要不是本应用（哪怕别的服务也返回
`{"ok": true}` 但没有 app 标识），**绝不打开浏览器**，并报「端口上应答的不是本应用，
请勿直接打开该地址」。若服务子进程在启动期就退出（bind 失败 / 端口竞态被抢），会**立即**
判定失败，不会傻等到超时。

**其它端口契约：**

- **探测覆盖双栈**：`127.0.0.1` 与 `[::1]` 都探测。若别家服务只监听 IPv6 回环，`localhost` 在 Windows/WebView 下会优先解析到 `::1`，只探 IPv4 会漏检。
- **本应用已占用 → 复用**：`/api/health` 的 **app 标识校验通过**才判定是自家服务，直接打开浏览器，不重复起服务。
- **`--strict-port` 显式 opt-in**：只有带该参数才「被占即报错」；默认自动顺延。

---

## 3. 首次启动要做什么

1. 启动后打开浏览器（默认 `http://127.0.0.1:8000`）。
2. 顶栏右侧点「设置」→ 选供应商 → 填 API Key → 点「测试连接」→ 选模型 → 「保存设置」。详见 [llm-providers.md](./llm-providers.md)。
3. **不配置也能用**：留空 API Key 时自动进入**降级模式**（本地启发式规则，不调用大模型），功能链路完整，只是内容质量下降、AI 问答与 AI 修改能力受限。顶栏会显示「LLM // 降级模式 · worker N」。
4. 切到 `01 资料` 填姓名与至少一段经历/技能，点「保存资料」，即可开始生成。

---

## 4. 本机环境坑：`http_proxy` 会让 localhost 请求返回 502

如果本机设了 `http_proxy` / `HTTP_PROXY`，代理会**拦截对 localhost 的请求**（表现为 502），导致本地服务与本地 LLM（如 Ollama）不可用。

- **命令行验证**：访问本机服务时绕过代理：

  ```bash
  curl --noproxy '*' http://127.0.0.1:8000/api/health
  ```

- **应用侧已处理**：
  - `start.py` 会为子进程注入 `NO_PROXY=localhost,127.0.0.1,::1,0.0.0.0`（若已有 `NO_PROXY` 则合并），启动日志会打印「已设置 NO_PROXY，本地地址将绕过系统代理」。
  - LLM 客户端（`backend/app/llm.py`）与设置页的「测试连接」（`backend/app/routers/settings.py`）都用 `trust_env=not is_local_url(...)`：**本机 / 内网地址直连、外部地址仍遵循系统代理**。判定函数是 `providers.is_local_url()`（识别 `localhost`、`127.0.0.1`、`::1`、`0.0.0.0`、`*.localhost`）。
  - 抓取招聘链接时同样如此（`fetch.py`）。
- **手动启动服务时**记得自己带上，例如：

  ```bash
  NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --port 8010
  ```

---

## 5. 数据存放与备份

| 项 | 值 |
|---|---|
| 数据库 | `data/app.db`（SQLite） |
| 位置来源 | `config.py`：`database_url = sqlite:///<项目根>/data/app.db`；可用环境变量 `DATABASE_URL` 覆盖 |
| 模式 | 连接时设 `PRAGMA journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`；`check_same_thread=False` 以便后台 worker 与请求线程共享 |
| 附带文件 | WAL 模式下还可能看到 `app.db-wal` / `app.db-shm` |
| 头像 | 以 data-url 内嵌在数据库里（不入独立文件） |

**备份建议**：

- 最稳妥：**停掉服务后**复制 `data/app.db`（连同 `app.db-wal`、`app.db-shm` 一起复制，或先手动执行一次 checkpoint）。
- 不要在服务运行中直接拷 `.db` 而漏掉 `-wal`，否则可能丢掉最近的事务。
- 想隔离实验，可用 `DATABASE_URL=sqlite:///绝对路径/别的库.db` 启动，避免污染真实库。

---

## 6. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 启动脚本报「需要 Python 3.11 或更高版本」 | 版本过低 | 升级 Python，或让 `py -3` / `python3` 指向 3.11+ |
| 依赖安装失败 | 网络 / 代理 / pip 源问题 | 检查网络；代理环境先设 `NO_PROXY` 或换 pip 源；再试 `python start.py --reinstall` |
| 端口被占用（默认自动顺延并提示实际端口） | 端口（默认 8000）被别的程序监听；探测含 IPv6 `[::1]` | 看启动日志里「已顺延到 <端口>」并按新端口访问；若要「被占即报错」加 `--strict-port`；也可 `python start.py --port <空闲端口>` 或结束占用进程。手动查占用者：`netstat -ano \| findstr :8000` |
| 提示「检测到服务已在运行」 | 该端口上跑的正是本应用 | 直接用已有服务；脚本只开浏览器不重复起 |
| 服务起了但浏览器打不开 | 实际端口不是 8000（用了 `--port` 或默认自动顺延） | 看启动日志里的「地址：…」/「将打开 http://…」，按那个端口访问；另注意 `localhost` 与 `127.0.0.1` 的双栈歧义（见上） |
| 页面能开但接口 502 | 本机代理拦截 localhost | 设 `NO_PROXY=localhost,127.0.0.1`；命令行用 `curl --noproxy '*'` |
| 点生成提示「岗位要求为空」 | JD 未填 | 在对话的「岗位要求」框粘贴 JD 后再生成（后端也会返回 400） |
| 生成报「尚未填写资料库…」 | 资料为空 | 到 `01 资料` 保存姓名 / 一段经历 / 技能 / 教育中的至少一项 |
| 生成失败（状态 chips 显示「失败」） | LLM 报错 / 输出被 `max_tokens` 截断 / 网络问题 | 点「重试」；或到设置调高「最大输出 token」、缩短岗位描述；查看 `#ws-stage` 上的错误文案 |
| 抓取链接返回 403 | 目标站点反爬 | 手动复制职位名与要求；详见 [02-features/url-import.md](../02-features/url-import.md) |
| 抓取返回 422 | 页面是职位列表 / 动态渲染 / 非网页 | 换职位详情页链接，或手动粘贴 |
| 设置里「测试连接」失败 | Key 无效 / Base URL 错 / 网络 | 看返回的中文结论（401/403=Key 无效，404/405=Base URL 不对，429=额度/频率，5xx=服务端）；详见 [llm-providers.md](./llm-providers.md) |

---

## 常见陷阱

1. **不要在用 `--reload` 的同时跑自动化测试**：改代码会触发重启，可能让测试中途断开。测试请用独立端口与隔离库（见 `05-testing/`）。
2. **`NO_PROXY` 只解决本机地址**：外部 LLM / 抓取外网仍可能走代理，这是预期行为（企业环境需要）。
3. **数据默认就在项目里**：`data/app.db` 会随项目目录一起被复制/删除，注意别误删；也注意别把它提交进版本库。
4. **`start.bat` 的暂停只在出错时出现**：正常退出不会停留。
5. **pip 装依赖失败不等于代码问题**：先确认能 `import fastapi, sqlmodel, httpx` 再怀疑代码。
