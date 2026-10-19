# 运维:桌面端安装包(Tauri 2 + Python sidecar)

> 这篇讲怎么把本项目打成一个 **Windows exe 安装包**,发给**没有 Python 环境**的用户双击即用;
> 以及桌面端和 web 端的关系、数据存在哪、出问题怎么查。
> 适合:负责打包发布的人、需要排查桌面端启动问题的开发者。

相关文件:`构建安装包.bat`、`desktop/`(Tauri 外壳)、`packaging/`(后端打包与重打包脚本)、
`desktop/src-tauri/tauri.conf.json`、`desktop/src-tauri/src/main.rs`、`packaging/server_entry.py`。

**重要前提:本项目以 web 为主。** 桌面端只是**额外的一层壳** —— `start.py` 那套 web 启动方式
完全保留、互不影响。桌面端存在的唯一目的是:把 Python 运行时和后端一起塞进安装包里,
让终端用户不需要装 Python。

---

## 1. 两种运行形态对照

| | web 端(开发/自用) | 桌面端(发给别人) |
|---|---|---|
| 启动 | `python start.py`(或 `start.bat`) | 双击安装后的 `AI Resume Generator.exe` |
| 需要 Python | **是**(脚本会自己建 venv) | **否**(Python 已打进 sidecar) |
| 访问方式 | 浏览器打开 `http://127.0.0.1:8000` | 应用窗口(内部加载同一套前端) |
| 后端进程 | `uvicorn app.main:app`(在 `.venv` 里) | `resume-server.exe`(PyInstaller 打包) |
| 数据目录 | `<项目根>/data/app.db` | `%LOCALAPPDATA%\AIResumeGenerator\data\app.db` |
| 前端来源 | `backend/app/static/`(FastAPI 直接托管) | 同左(打进 sidecar,由 sidecar 托管) |

**两者数据完全独立** —— 桌面端通过环境变量 `APP_DATA_DIR` 指向用户目录,不会碰项目里的 `data/`。
反过来 web 端也不会读写桌面端的库。想让两边看到同一份数据,用「设置」里的导入/导出或手动拷 `app.db`。

---

## 2. 架构:为什么是「Tauri 外壳 + Python sidecar」

```
安装包(NSIS)
└── AI Resume Generator.exe        ← Tauri 2 窗口(约 4.6 MB)
    ├─ spawn → resume-server.exe    ← PyInstaller 打包的 FastAPI(约 21 MB,含 Python 运行时)
    │            --port 0           自选空闲端口
    │            stdout: APP_READY <port>
    │            并写 {data}/port.txt(兜底)
    └─ 拿到端口后轮询 /api/health
         校验 JSON 里 app == "ai-resume-generator" 且 ok == true
         ✔ 通过 → 窗口导航到 http://127.0.0.1:<port>/
         ✘ 失败 → 把占位页替换成可读错误(不弹系统对话框)
    退出时 kill sidecar
```

几个关键设计及原因:

| 设计 | 为什么 |
|---|---|
| 窗口加载 `http://127.0.0.1:<port>` 而不是内嵌静态文件 | 前端所有请求都是**绝对路径**(`/api/...`、`/static/...`)。同源加载后**前端一行都不用改**就能复用 |
| sidecar 用 `--port 0` 动态选端口 | 固定端口会被别的程序占用(参见 [`getting-started.md`](./getting-started.md) 的端口行为) |
| 打开窗口前**强校验 `app` 字段** | 只看 HTTP 200 会把「别人的服务」当成本应用 —— 这是本项目反复强调的一点 |
| 退出时 kill sidecar,sidecar 也监听 stdin EOF | 双保险。GUI 被强杀(`taskkill /F`)时 stdin 关闭,sidecar 会自行退出 |
| 窗口用 `maximized: true` 而非 fullscreen | fullscreen 会隐藏标题栏,用户就没法关闭窗口了 |

### 启动耗时参考

| 阶段 | 典型耗时 |
|---|---|
| PyInstaller onefile 解压 + 启动 | ~3.8 s |
| 端口就绪 + 健康校验 + 导航 | ~2.8 s |
| **合计到页面可用** | **约 6.6 s** |

其中约 1.2 s 是「先尝试运行时 `navigate()`,不生效再重建窗口」的验证窗口 —— 详见第 6 节。

---

## 3. 构建安装包

### 一键构建

```bat
构建安装包.bat
```

双击运行即可(会 `chcp 65001` 以正确显示中文)。**首次构建需要 Rust 编译,约 10-20 分钟**,
脚本每 15 秒打印一次心跳(`... 仍在构建(Rust 首次编译较慢,请耐心等待) 已用 X 分 Y 秒`),
避免用户误以为卡死而强杀窗口。

### 五个阶段

| 阶段 | 做什么 | 失败会怎样 |
|---|---|---|
| **1/5 检查构建环境** | 查 `node`/`npm`/`rustc`/`cargo` 和 `.venv/Scripts/python.exe` | 列出缺什么 + 安装地址,退出码 1 |
| **2/5 准备构建依赖** | 缺 `pyinstaller`/`pillow` 才装;`icons/icon.ico` 不存在才用 `make_icon.py` 生成并 `npx tauri icon` 出全套图标 | 报错并暂停 |
| **3/5 打包后端** | PyInstaller 出 `packaging/out/resume-server.exe`,再按 `rustc -vV` 的 host triple 重命名为 `binaries/resume-server-<triple>.exe` | 报错(`sidecar` 命名必须带 triple,Tauri 靠它匹配) |
| **4/5 构建 NSIS** | `tauri_build.py` 托管 `npm run tauri build`(带心跳 + 真实退出码),产出 `bundle/nsis/*.exe` | 报错;旧安装包会被还原,不会让用户丢掉上一个能用的包 |
| **4.5 改默认安装目录** | `repack_nsis.py`:改写 Tauri 生成的 `installer.nsi` 的 `$INSTDIR`,用 `makensis` 重编译并覆盖产物 | 失败只警告,**保留 tauri 原包**(默认目录回退到用户目录) |
| **5/5 报告产物** | 用通配符 + 按修改时间取最新产物,打印大小与路径,询问是否启动安装程序 | 零命中时报「本次未产出」,**绝不启动旧包** |

### 产物位置

| 产物 | 路径 | 大小 |
|---|---|---|
| **NSIS 安装包(发这个)** | `desktop/src-tauri/target/release/bundle/nsis/AI Resume Generator_<版本>_x64-setup.exe` | 约 22.7 MiB |
| 未安装的主程序 | `desktop/src-tauri/target/release/ai-resume-generator-desktop.exe` | 约 4.6 MB |
| 后端 sidecar | `packaging/out/resume-server.exe` | 约 21.2 MB |

**所有产物都固定在项目内**。这一点有专门的保护:`packaging/tauri_build.py` 会把
`CARGO_TARGET_DIR` 覆盖成 `desktop/src-tauri/target` —— 因为不少开发机的**用户级环境变量**里
设了全局 `CARGO_TARGET_DIR`(指向别的项目),不覆盖的话产物会写进**别人的项目目录**,
既违反「产物在本项目内」的约定,也容易和别人的构建产物混在一起。

### 默认安装目录

默认安装到 **`D:\AI Resume Generator`**(与 GitHub 仓库名一致)。

实现见 `packaging/repack_nsis.py`:Tauri 生成的 `installer.nsi` 是**完全展开**的
(所有 `File` 指令都是绝对路径),脚本只替换一行

```nsis
StrCpy $INSTDIR "$LOCALAPPDATA\${PRODUCTNAME}"   ; Tauri 原值
StrCpy $INSTDIR "D:\${PRODUCTNAME}"              ; 本项目定制
```

再用 Tauri 缓存的 `%LOCALAPPDATA%\tauri\NSIS\makensis.exe` 重新编译。这样做的好处是
**不用维护整份 32 KB 模板**,Tauri 升级后模板变化也不会让脚本失效;找不到预期行时脚本会
警告并跳过,而不是硬改出错的安装包。

> **为什么用户会看到「默认还是上次那个目录」**:NSIS 模板里有 `RestorePreviousInstallLocation`,
> 它会从注册表读回上一次的安装路径并**优先复用**。键是
> `HKCU\Software\<MANUFACTURER>\<产品名>`(值名是空串)。所以只要有人用
> `/D=D:\某个目录` 装过一次,之后**不带参数也会沿用该目录**。
> 想恢复默认行为,删掉这个键即可:
> ```python
> import winreg
> winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\justine\AI Resume Generator")
> ```

### 静默安装(自动化测试用)

```bat
:: /D= 必须放最后,且路径不加引号
"AI Resume Generator_1.0.0_x64-setup.exe" /S /D=D:\some\dir
```

卸载:`<安装目录>\uninstall.exe /S`。

---

## 4. 安装包里有什么

| 文件 | 作用 |
|---|---|
| `ai-resume-generator-desktop.exe` | Tauri 窗口 + 启动编排(Rust) |
| `resume-server.exe` | FastAPI 后端 + 内置 Python 运行时(PyInstaller onefile) |
| `uninstall.exe` | NSIS 卸载程序 |

安装信息写在注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\AI Resume Generator`。
`installMode` 是 `currentUser` —— **不需要管理员权限**,装到用户目录(默认 D 盘)。
安装向导语言:简体中文 / English(不弹语言选择框)。

---

## 5. 桌面端的数据与日志

| 内容 | 路径 |
|---|---|
| 数据库 | `%LOCALAPPDATA%\AIResumeGenerator\data\app.db` |
| WAL 文件 | 同目录 `app.db-wal` / `app.db-shm`(SQLite WAL 模式,正常存在) |
| 端口文件 | 同目录 `port.txt`(sidecar 写,Rust 读,stdout 协议失败时兜底) |
| **启动日志** | 同目录 `launch.log` |
| WebView2 用户数据 | `%LOCALAPPDATA%\com.justine.ai-resume-generator\EBWebView` |

**`launch.log` 是排查桌面端问题的第一现场**。release 构建是 `windows_subsystem="windows"`,
**没有控制台输出**,没这份日志就只能盲猜。它逐行记录了:

- 数据目录解析结果、sidecar spawn 结果与 pid
- sidecar 的 stdout/stderr 原文、`APP_READY` 解析结果
- 每次健康探测的结论(`Ours` / `NotOurs` / `Pending + 原因`)
- 导航前后窗口真实 URL、用了哪种导航方式(`navigate()` / `rebuild`)
- `ui.fail()` 被调用的位置与消息

例:

```
[1792413699095] start_backend: sidecar spawn 成功（pid=18356）
[1792413700504] stdout: "APP_READY 59050"
[1792413700504] parse_ready_line -> Some(59050)
[1792413700516] probe: Ours（身份校验通过）
[1792413700517] navigate: 目标 http://127.0.0.1:59050/，当前 url = http://tauri.localhost/
[1792413722076] wait_for_url: 超时未跳转（…实际 http://tauri.localhost/）
[1792413722077] navigate: 运行时导航未生效，改为重建窗口（初始 URL 即目标地址）
[1792413733900] navigate: 校验通过（重建窗口）
```

数据备份:直接拷 `app.db`(建议先退出应用,让 WAL 合并)。

---

## 6. 常见陷阱

### ① `WebviewWindow::navigate()` 返回 `Ok` 但窗口没跳转

Tauri 2 的 `navigate()` 是**投递式**的:消息进了 dispatcher 就返回 `Ok`,webview 内部拒绝跳转它并不知道。
实测在 `http://tauri.localhost/`(占位页源)上,运行时导航和页面自己发起的 `location.replace()`
**都不生效**,窗口被钉在原地。

**解决**:先给 `navigate()` 一个很短的验证窗口(1.2 s,真能跳是毫秒级),并**回读 `window.url()` 确认**;
不成就用 `WebviewUrl::External(url)` **重建窗口**(初始 URL 即目标,绕开运行时导航),
沿用原窗口位置/尺寸/最大化状态,再关掉占位窗口。`launch.log` 里会记成 `导航成功（rebuild）`。

**配套教训**:URL 变了 ≠ 页面渲染了。别只用 `current_url()` 判定成功。

### ② 先 `mark_ready()` 再 `navigate()` 会吞掉错误

如果先标记「已就绪」再导航,导航失败时 `ui.fail()` 会因为 `is_ready()` 的早退判断被**静默丢弃** ——
表现是「既没跳转、也不报错、就一个白窗口」。正确顺序是**导航成功之后才** `mark_ready()`。

### ③ 强杀 GUI 会让 WebView2 profile 被锁,下次白屏

用 `taskkill /F` 杀掉 `ai-resume-generator-desktop.exe` 时,`msedgewebview2.exe` 子进程可能变成孤儿,
继续占用 `EBWebView` 目录。下次启动时 WebView2 初始化失败 → Tauri 只给一个纯白 Surface、
**不报错**(而 `launch.log` 里可能还写着「导航成功」,因为 URL 确实变了)。

排查与恢复:

```bat
:: 注意：只杀 sidecar 不够，必须连 webview2 一起清
taskkill /F /IM ai-resume-generator-desktop.exe
taskkill /F /IM msedgewebview2.exe
:: 仍然白屏时，再删掉 profile（会丢失 WebView2 的缓存，不影响业务数据）
rmdir /S /Q "%LOCALAPPDATA%\com.justine.ai-resume-generator"
```

> 正常关闭窗口不会触发这个问题 —— 只有强制终止才会。自动化测试要注意:**别用强杀的方式收尾**。

### ④ 构建产物跑到别的项目目录

症状:`tauri build` 打印的产物路径是 `D:\<别的项目>\rust\target\...`。
原因:用户级环境变量 `CARGO_TARGET_DIR` 指向别处。**必须通过 `构建安装包.bat` 构建**
(它经 `tauri_build.py` 覆盖该变量);手动敲 `npm run tauri build` 会绕过这个保护。

### ⑤ 构建命令需要 Python 来托管,不能用 PowerShell

`tauri_build.py` 存在的原因是:批处理自身做不到「后台跑 + 定时心跳 + 拿到真实退出码」三件事同时成立
(`start /b` 拿不到退出码,`call` 又阻塞到没有任何心跳);而 PowerShell 的 `Start-Process` 在本机会
因系统同时设了 `HTTPS_PROXY` 与 `https_proxy`(Windows 环境块**大小写不敏感**)而抛
`Item has already been added. Key in dictionary: 'HTTPS_PROXY'`。
Python 的 `subprocess` 不重建环境字典,因此不受影响。

### ⑥ 前端资源没打包进去 → 窗口空白或报 404

`tauri.conf.json` 的 `frontendDist` 指向 `../dist`,里面只有一个**占位页**
(显示「正在启动」)。真正的界面由 sidecar 托管。所以:

- `desktop/dist/index.html` 是**源码**,必须提交;`desktop/dist/` 其余内容不忽略、但通常为空;
- 若把 `desktop/dist/` 整体写进 `.gitignore`,克隆后构建会因缺占位页而失败。

### ⑦ `resume-server` 出现两个同名进程是正常的

PyInstaller onefile 的 bootloader 会 **父进程 + re-exec 子进程**,两个都叫 `resume-server.exe`。
不是孤儿进程。

### ⑧ Windows 上不要依赖 `wmic` / PowerShell 检查进程

新版 Windows 已移除 `wmic`(`FileNotFoundError [WinError 2]`);PowerShell 的 `Add-Type`
可能被安全策略拦截。用 `ctypes` + `CreateToolhelp32Snapshot`(`PROCESSENTRY32W`)枚举父进程最稳。

---

## 7. 验收清单(发出去之前)

```text
[ ] 构建安装包.bat 退出码 0，报告了安装包路径与大小
[ ] 清掉注册表记忆 + 删掉旧目录，模拟「从没装过」的用户
[ ] 静默安装 /S → 落在 D:\AI Resume Generator（C 盘未被创建）
[ ] 双击启动 → 窗口最大化、能看到应用界面（不是「正在启动」占位页）
[ ] launch.log 里有：probe: Ours → 导航成功（navigate() 或 rebuild）
[ ] 在应用里新建一条对话 → 关闭 → 重新启动 → 那条对话还在（数据持久化）
[ ] 卸载 /S → 安装目录清空、无残留进程
```

**「窗口真的显示了应用」的可靠判定**(不要只看进程活着或 health 200):

1. `msedgewebview2.exe` 是该 exe 的**子进程**(用 ctypes 查 `ParentProcessId`);
2. sidecar 端口上存在来自 webview 的 **ESTABLISHED 连接**(前端 `boot()` 会建 `/ws/tasks` 长连接,
   零连接 = 页面没加载);
3. 页面自报:`window.eval("document.title")` 应是应用的标题,而不是占位页的「正在启动」。

> 不可靠的判定手段(都实测踩过):`PrintWindow` 抓 WebView2 常返回纯白;
> `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 的远程调试端口在本机不生效。
