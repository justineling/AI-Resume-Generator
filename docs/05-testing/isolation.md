# 测试隔离机制

本项目的测试**默认全部跑在隔离数据库**上，绝不碰用户真实库 `data/app.db`。
这一约束由两个小模块保证：`backend/tests/_ui_server.py`（浏览器套件用）与
`backend/tests/_dbguard.py`（进程内套件用）。

---

## 一、为什么必须隔离

早期 `ui_check` / `e2e_verify` / `chat_e2e` 等脚本默认打 `http://127.0.0.1:8000`，
而 8000 是**用户开发服务**、用的是**用户真实库** `data/app.db`。
跑一次走查就会往用户库里写几十上百条测试对话——**实测单日污染 200+ 条**，
清理起来非常麻烦（`data/app.db.bak_pollution2_1791718748` 就是当时留下的污染备份）。

根因有两层：

1. **浏览器套件**：早期默认 `BASE_URL=http://127.0.0.1:8000`，连到了开发服务；
2. **进程内套件**：用 `TestClient(app)` 时会打到 `settings.database_url` 解析出的库。
   若忘记设 `DATABASE_URL`，默认就是共享库 `data/app.db`。

因此两条链路都改成「**不显式指定就自动隔离**」，把「忘记设环境变量」这个最常见的失误兜住。

---

## 二、进程内隔离：`_dbguard.py`

进程内测试（`smoke` / `regression` / `progress_probe`）使用：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _dbguard import use_isolated_db, warn_if_shared_db

use_isolated_db("regression")        # 必须在 import app.* 之前调用！

import app.scheduler as sched_mod
from app.main import app
...
```

### `use_isolated_db(script_name)` 做什么

- 若 `DATABASE_URL` **已设置**：原样保留；若其指向共享库 `data/app.db`，打印醒目警告（尊重显式意图）。
- 若 `DATABASE_URL` **未设置**：自动设为 `sqlite:///data/_<script_name>.db`，并**先删除**该文件
  （含 `-wal` / `-shm`）保证每次运行起点干净、测试幂等。返回最终的 URL。
- 打印一行 `[隔离库] 未指定 DATABASE_URL，自动使用 ... （不会写入用户库）`。

### 为什么**必须**在 `import app.*` 之前调用

`app.config.settings` 是一个**在导入时就读取环境变量**的配置对象：

```
app/config.py:  settings = Settings()   # 导入即解析 DATABASE_URL / LLM_* 等
```

一旦 `app.config`（或任何 `import app.*` 触发的模块）被导入，`settings.database_url`
就已经定死。此后即使再改 `os.environ["DATABASE_URL"]` 也为时已晚——
数据库引擎已经绑定到旧地址，测试数据仍会写进共享库。

所以 `use_isolated_db()` 的调用位置被刻意放在所有 `import app.*` **之前**，
并在脚本里用注释反复强调这一点（见 `smoke.py`、`regression.py` 第 20~34 行）。

> 补充：`app/db.py` 的 engine 也是在导入时按 `settings.database_url` 创建的，
> 同样属于「导入期冻结」。这就是「必须在 import 前设好环境变量」的第二个原因。

### `warn_if_shared_db(script)`

在 `main()` 里调用一次，读取**已解析**的 `settings.database_url`：

- 若它仍指向共享库 `data/app.db` → 打印醒目警告（**只提示，不阻断**，保持向后兼容）；
- 若指向隔离库 → 静默。

判定依据是「解析后的地址」而不是「环境变量是否存在」，所以即使 `DATABASE_URL` 未设置、
但 `.env` 把它指到了别处，也不会误报。

---

## 三、浏览器隔离：`_ui_server.py`

浏览器套件（`ui_check` / `chat_e2e` / `e2e_verify` / `iv_check` / `ux_check`）用
`_ui_server` 自起一个**隔离库 + 独立端口**的 uvicorn 子进程。

### 端口分配

- 每个套件使用固定但互不相同的端口：8121(`ui_check`) / 8122(`e2e_verify`) /
  8125(`chat_e2e`) / 8126(`iv_check`) / 8127(`ux_check`)；
  `xss_probe`=8123、`settings_ui_check`=8124 各自在脚本内起子进程。
- 启动前用 `_port_free(port)`（`bind` 试探 + `SO_REUSEADDR`）检查端口是否被占用，
  被占用则报错（避免误连到别的服务）。
- **绝不使用 8000**（用户开发服务端口）。

### `spawn_isolated_server(name, port)`

返回 `(proc, base_url, db_file)`：

1. 计算隔离库 `data/_ui_<name>.db`，删除同名 `-wal` / `-shm`，保证干净起点；
2. 检查端口空闲；
3. 复制当前环境并设置：
   - `DATABASE_URL = sqlite:///<data/_ui_<name>.db>`
   - `NO_PROXY` / `no_proxy = "localhost,127.0.0.1"`（本机 `http_proxy` 会拦截 localhost）；
4. `subprocess.Popen` 启动 `uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port <port>`，
   `cwd=项目根`，stdout/stderr 丢弃；
5. 轮询 `GET /api/health` 直到就绪（默认 25s），失败则清理并报错；
6. 打印 `[隔离服务] <base>  DB=_ui_<name>.db（不会写入用户库）`。

### `stop_isolated_server(proc, db_file)`

`terminate()` → 等 10s → 失败则 `kill()`，然后删除数据库文件（含 `-wal` / `-shm`）。
可重复调用（幂等）。

### `isolated_server(name, port)`（上下文管理器）

`with isolated_server("ux_check", 8127) as base:` —— 进入时 `spawn`，退出时（含异常）
`stop`。多数浏览器套件用这个。

### 为什么 `ui_check.py` 用 `os._exit()`，且必须在退出前显式 stop

`ui_check.py` 收尾处**故意**调用 `os._exit(code)`：

```python
# headless Edge 在本机偶发在关闭期挂起 driver 连接（与页面逻辑无关），
# 故不等待浏览器优雅关闭，直接以最终结论退出。
code = 1 if errors else 0
if on_exit:
    on_exit()          # ← 必须先显式停掉隔离服务
os._exit(code)         # ← 跳过 with/finally 清理
```

`os._exit()` **立即终止进程、不执行任何 `atexit` / `finally` / `with` 的 `__exit__`**。
如果直接把 `isolated_server` 写成 `with` 用法，`os._exit()` 会让子进程 uvicorn
变成孤儿进程（继续占用端口）且隔离库文件不会被删除。

因此 `ui_check.py` 改成手动 `spawn_isolated_server(...)`，并把
`stop_isolated_server(proc, db_file)` 作为 `on_exit` 回调，在 `os._exit()` **之前**显式调用。
（`main()` 里还额外用 `try/except BaseException` 兜底：非 `os._exit` 的异常路径也会 stop。）

---

## 四、如何验证隔离是否生效

### 1. 观察脚本自身的提示

- 进程内套件应打印：`[隔离库] 未指定 DATABASE_URL，自动使用 sqlite:///.../_<脚本>.db（不会写入用户库）`；
- 浏览器套件应打印：`[隔离服务] http://127.0.0.1:<port>  DB=_ui_<name>.db（不会写入用户库）`。

若看到 `[警告] ... 共享库 data/app.db`，说明隔离没生效，立即停止并检查 `DATABASE_URL`。

### 2. 对比测试前后用户库的 chat 行数（最硬的证据）

以只读方式打开真实库，统计 `chat` 行数；跑完测试套件后再统计一次，两次必须**一致**：

```bash
# 用只读连接，绝不产生写事务（避免 WAL 变化）
./.venv/Scripts/python.exe - <<'PY'
import sqlite3
uri = "file:<项目绝对路径>/data/app.db?mode=ro"
con = sqlite3.connect(uri, uri=True)
print("chat =", con.execute("select count(*) from chat").fetchone()[0])
con.close()
PY
```

或者直接用 sqlite3 命令行：

```bash
sqlite3 "file:data/app.db?mode=ro" "select count(*) from chat;"
```

**判据**：测试前后该数字相等即隔离生效；若变大，说明某个套件连到了真实库。

> 建议同时观察 `chatmessage` / `resume` 的行数，作为交叉验证。

---

## 五、常见坑

- **忘记 `NO_PROXY`** → localhost 请求被 `http_proxy` 拦截，测试超时/误报；
  隔离服务和测试脚本都要放行 localhost（`_ui_server` 已内置）。
- **`use_isolated_db()` 放在 `import app.*` 之后** → 隔离失效，污染真实库。
- **显式把 `BASE_URL` / `DATABASE_URL` 指向 8000 / `data/app.db`** → 脚本会打印警告，
  尊重你的意图但确实会污染，除调试外不要这么做。
- **端口被占** → `spawn_isolated_server` 会直接报错而不是换端口，
  避免误连到非本套件的服务；关掉占用端口的进程即可。
