//! AI Resume Generator —— 桌面外壳（Tauri 2）。
//!
//! 职责：
//!   1. 启动时 spawn 随包分发的 Python 后端 sidecar（`resume-server.exe --port 0`）；
//!   2. 从 sidecar 的 stdout 读取就绪协议行 `APP_READY <port>`；
//!   3. 轮询 `http://127.0.0.1:<port>/api/health`，**强校验** `app == "ai-resume-generator"`
//!      后才把窗口导航到该地址（只看 HTTP 200 会把「别人的服务」当成本应用）；
//!   4. 失败时把占位页替换成可读错误（不弹系统对话框）；
//!   5. 退出时 kill sidecar，避免留下孤儿进程。
//!
//! 为什么用动态端口：固定端口可能被别的程序占用；动态端口 + 强身份校验，
//! 既不会「打开别人的页面」，也不会因为端口冲突而启动失败。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use tauri::{AppHandle, Manager, RunEvent, WebviewWindow};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// 与 `backend/app/main.py` 的 `APP_ID` 必须一致（跨进程无法共享常量）。
const APP_ID: &str = "ai-resume-generator";

/// 与 `packaging/server_entry.py` 约定的就绪协议前缀。
const READY_PREFIX: &str = "APP_READY ";

/// 从 spawn 到「页面可交互」的总超时。
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);

/// 拿到端口后等待健康检查通过的最长时间。
const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);

/// 健康检查轮询间隔。
const HEALTH_INTERVAL: Duration = Duration::from_millis(300);

/// 单次健康检查请求超时。
const HEALTH_REQUEST_TIMEOUT: Duration = Duration::from_secs(2);

// --------------------------------------------------------------------------- //
// 启动日志
//
// 为什么需要它：release 构建是 `windows_subsystem = "windows"`，**没有控制台**，
// 所有 `eprintln!` 都被丢进黑洞。一旦「窗口停在占位页」，仅凭进程存活无法判断
// 卡在哪一步（spawn 失败？没读到 APP_READY？健康检查没过？navigate 报错？）。
// 这里用一个不依赖任何 crate 的极简追加写文件日志，把每一步都留痕。
// --------------------------------------------------------------------------- //

/// 数据目录的进程级缓存（日志也要写到同一处）。
static DATA_DIR: OnceLock<PathBuf> = OnceLock::new();

/// 解析用户级数据目录（与 `backend/app/config.py` 的 `resolve_data_dir()` 保持一致）。
///
/// 优先级：`APP_DATA_DIR` → `%LOCALAPPDATA%\AIResumeGenerator\data`。
/// 打包后绝不能落到 exe 所在目录（Program Files 不可写）或 `_MEIPASS`（每次启动都变）。
fn resolve_data_dir() -> PathBuf {
    if let Ok(value) = std::env::var("APP_DATA_DIR") {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    let base = std::env::var("LOCALAPPDATA")
        .or_else(|_| std::env::var("APPDATA"))
        .unwrap_or_else(|_| ".".to_string());
    PathBuf::from(base).join("AIResumeGenerator").join("data")
}

/// 取（并缓存）数据目录。
fn data_dir() -> &'static Path {
    DATA_DIR.get_or_init(resolve_data_dir)
}

/// 追加一行启动日志到 `<data_dir>/launch.log`。
///
/// 写文件失败一律忽略——日志只是诊断手段，绝不能因为它让应用起不来。
fn log_launch(msg: &str) {
    let dir = data_dir();
    let _ = std::fs::create_dir_all(dir);
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("launch.log"))
    {
        let _ = writeln!(file, "[{millis}] {msg}");
        let _ = file.flush();
    }
}

/// sidecar 子进程句柄，退出时用于 kill。
#[derive(Default)]
struct SidecarState(Mutex<Option<CommandChild>>);

/// `/api/health` 的应答结构（只关心身份校验需要的两个字段）。
#[derive(Debug, Deserialize)]
struct HealthPayload {
    #[serde(default)]
    ok: bool,
    #[serde(default)]
    app: String,
}

/// 单次健康探测的结果。
enum Probe {
    /// 确认是本应用且已就绪。
    Ours,
    /// 端口上有服务应答，但不是本应用（携带对方自报的 app 值）。
    NotOurs(String),
    /// 还没起来 / 连接被拒 / 应答不可解析，继续轮询。
    Pending(String),
}

/// 占位页 UI 控制：只在「未就绪」时允许报错，避免把错误文本注入到真实应用页面里。
#[derive(Clone)]
struct Ui {
    window: WebviewWindow,
    /// 已经成功导航到真实应用页面。
    ready: Arc<AtomicBool>,
    /// 已经报过错（错误只报一次）。
    failed: Arc<AtomicBool>,
    /// 健康检查/导航流程已被某个线程认领（避免 stdout 与端口文件两条路径重复执行）。
    claimed: Arc<AtomicBool>,
}

impl Ui {
    fn new(window: WebviewWindow) -> Self {
        Self {
            window,
            ready: Arc::new(AtomicBool::new(false)),
            failed: Arc::new(AtomicBool::new(false)),
            claimed: Arc::new(AtomicBool::new(false)),
        }
    }

    /// 是否已经成功导航到真实应用页面。
    fn is_ready(&self) -> bool {
        self.ready.load(Ordering::SeqCst)
    }

    /// 认领「启动流程」；只有第一个调用者拿到 true。
    fn claim(&self) -> bool {
        !self.claimed.swap(true, Ordering::SeqCst)
    }

    /// 标记成功。
    ///
    /// 注意：**只有在 navigate 真正成功后才调用**。反过来（先 ready 再 navigate）
    /// 会让 navigate 的失败被 `fail()` 的 `is_ready()` 早退吞掉，表现为
    /// 「窗口永远停在占位页且没有任何错误提示」——一个非常难查的静默失败。
    fn mark_ready(&self) {
        self.ready.store(true, Ordering::SeqCst);
        log_launch("mark_ready: 已标记就绪");
    }

    /// 把占位页替换成可读错误；幂等，且就绪后不再干预页面。
    fn fail(&self, where_: &str, message: &str) {
        log_launch(&format!("ui.fail @ {where_}: {message}"));
        if self.is_ready() {
            log_launch("ui.fail: 已就绪，忽略（不覆盖真实页面）");
            return;
        }
        if self.failed.swap(true, Ordering::SeqCst) {
            log_launch("ui.fail: 之前已报过错，忽略");
            return;
        }

        let window = self.window.clone();
        let payload = serde_json::to_string(message)
            .unwrap_or_else(|_| "\"后端启动失败（错误信息无法序列化）\"".to_string());
        // 占位页可能在窗口完成首帧前就被判失败，故稍等再注入，并重试几次。
        thread::spawn(move || {
            for attempt in 0..5 {
                thread::sleep(Duration::from_millis(if attempt == 0 { 250 } else { 400 }));
                let script = format!(
                    r#"(function () {{
  try {{
    document.body.classList.add('is-error');
    var title = document.getElementById('msg');
    if (title) {{ title.textContent = '后端服务启动失败'; }}
    var hint = document.getElementById('hint');
    if (hint) {{ hint.textContent = '请关闭本窗口后重试；若持续失败，请检查杀毒软件是否拦截了 resume-server.exe。'; }}
    var box = document.getElementById('err');
    if (box) {{
      box.hidden = false;
      box.textContent = {payload};
    }}
    document.title = 'AI Resume Generator · 启动失败';
    return 'injected';
  }} catch (e) {{ return 'error: ' + e; }}
}})();"#,
                    payload = payload
                );
                if window.eval(&script).is_ok() {
                    log_launch(&format!("ui.fail: 错误文本已注入占位页（第 {} 次尝试）", attempt + 1));
                    break;
                }
            }
        });
    }
}

/// 读取 sidecar 写下的端口文件（stdout 协议解析失败时的兜底）。
fn read_port_file(path: &Path) -> Option<u16> {
    let text = std::fs::read_to_string(path).ok()?;
    text.trim().parse::<u16>().ok()
}

/// 解析一行 stdout，若是就绪协议则返回端口。
fn parse_ready_line(line: &str) -> Option<u16> {
    let rest = line.trim().strip_prefix(READY_PREFIX)?;
    rest.trim().parse::<u16>().ok()
}

/// 单次健康探测。
fn probe(client: &reqwest::blocking::Client, port: u16) -> Probe {
    let url = format!("http://127.0.0.1:{port}/api/health");
    let response = match client.get(&url).send() {
        Ok(resp) => resp,
        Err(err) => {
            let reason = format!("连接失败：{err}");
            log_launch(&format!("probe: Pending（{reason}）"));
            return Probe::Pending(reason);
        }
    };

    let status = response.status();
    if !status.is_success() {
        // 我们的 /api/health 恒返回 200；非 2xx 说明对面可能是别的服务，
        // 也可能本应用尚未挂载完路由，继续轮询。
        let reason = format!("HTTP {}", status.as_u16());
        log_launch(&format!("probe: Pending（{reason}）"));
        return Probe::Pending(reason);
    }

    match response.json::<HealthPayload>() {
        Ok(payload) => {
            if payload.app == APP_ID && payload.ok {
                log_launch("probe: Ours（身份校验通过）");
                Probe::Ours
            } else if payload.app.is_empty() {
                log_launch("probe: Pending（应答缺少 app 字段）");
                Probe::Pending("应答缺少 app 字段".to_string())
            } else {
                log_launch(&format!("probe: NotOurs（app = {:?}）", payload.app));
                Probe::NotOurs(payload.app)
            }
        }
        // 200 但不是我们的 JSON —— 端口上确实是别的服务
        Err(err) => {
            log_launch(&format!("probe: NotOurs（非预期应答 {err}）"));
            Probe::NotOurs(format!("非预期应答（{err}）"))
        }
    }
}

/// 轮询健康检查，直到确认是本应用。
fn wait_for_health(client: &reqwest::blocking::Client, port: u16) -> Result<(), String> {
    let deadline = Instant::now() + HEALTH_TIMEOUT;
    let mut last = String::from("（尚未收到应答）");

    while Instant::now() < deadline {
        match probe(client, port) {
            Probe::Ours => return Ok(()),
            Probe::NotOurs(app) => {
                return Err(format!(
                    "端口 {port} 上应答的不是本应用（app = \"{app}\"）。\n\
                     可能是别的程序抢先占用了该端口，请关闭它后重试。"
                ));
            }
            Probe::Pending(reason) => last = reason,
        }
        thread::sleep(HEALTH_INTERVAL);
    }

    Err(format!(
        "后端在 {} 秒内没有通过健康检查。\n最后一次探测结果：{last}",
        HEALTH_TIMEOUT.as_secs()
    ))
}

/// 读取窗口当前 URL（同步 getter，走 dispatcher）。
fn current_url(window: &WebviewWindow) -> String {
    match window.url() {
        Ok(url) => url.to_string(),
        Err(err) => format!("<读取失败：{err}>"),
    }
}

/// 轮询等待窗口 URL 变成期望值。
///
/// `navigate()` 是**投递式**的：一旦消息进了 dispatcher 就返回 Ok，webview 内部
/// 失败它并不知情。所以必须用 `url()` 回读校验，否则「没跳转」会被当成成功
/// —— 这正是本模块第一版的真实 bug（navigate 返回 Ok，窗口却永远停在占位页）。
fn wait_for_url(window: &WebviewWindow, expected: &str, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        let actual = current_url(window);
        if actual.starts_with(expected) {
            return true;
        }
        if Instant::now() >= deadline {
            log_launch(&format!(
                "wait_for_url: 超时未跳转（期望以 {expected} 开头，实际 {actual}）"
            ));
            return false;
        }
        thread::sleep(Duration::from_millis(150));
    }
}

/// 把窗口导航到真实应用地址，并**验证**确实跳过去了。
///
/// ## 为什么不能只靠 `navigate()`
///
/// 实测（launch.log 记录）：
/// ```text
/// navigate: 目标 http://127.0.0.1:52677/，当前 url = http://tauri.localhost/
/// navigate: navigate() 已投递，回读校验中…
/// wait_for_url: 超时未跳转（期望以 http://127.0.0.1:52677/ 开头，实际 http://tauri.localhost/）
/// navigate: eval(location.replace) 已投递，回读校验中…
/// wait_for_url: 超时未跳转（实际 http://tauri.localhost/）
/// navigate: 校验通过（重建窗口）
/// ```
/// 即：**`navigate()` 返回 Ok，但窗口 URL 根本没变**；连页面自己发起的
/// `location.replace()` 也不生效（webview 被钉在本地资源源上）。
/// 而「新建一个窗口、初始 URL 就是目标地址」是确定有效的。
///
/// 所以策略是：先给 `navigate()` 一个很短的验证窗口（万一将来版本允许，
/// 就能省掉重建窗口），不成就**直接重建窗口**，不做无谓的重试。
fn navigate(app: &AppHandle, window: &WebviewWindow, port: u16) -> Result<&'static str, String> {
    let url_text = format!("http://127.0.0.1:{port}/");
    log_launch(&format!(
        "navigate: 目标 {url_text}，当前 url = {}",
        current_url(window)
    ));

    if let Ok(url) = url_text.parse::<tauri::Url>() {
        match window.navigate(url) {
            Ok(()) => log_launch("navigate: navigate() 已投递，回读校验中…"),
            Err(err) => log_launch(&format!("navigate: navigate() 返回错误：{err}")),
        }
        // 1.2 秒足够：真能跳的话是毫秒级
        if wait_for_url(window, &url_text, Duration::from_millis(1200)) {
            log_launch("navigate: 校验通过（navigate()）");
            return Ok("navigate()");
        }
    } else {
        log_launch(&format!("navigate: 解析地址 {url_text} 失败"));
    }

    // 运行时导航不可靠 → 直接重建窗口（初始 URL 即目标地址，确定有效）
    log_launch("navigate: 运行时导航未生效，改为重建窗口（初始 URL 即目标地址）");
    match rebuild_window(app, &url_text) {
        Ok(()) => {
            log_launch("navigate: 校验通过（重建窗口）");
            Ok("rebuild")
        }
        Err(err) => {
            let actual = current_url(window);
            log_launch(&format!("navigate: 重建窗口也失败：{err}"));
            Err(format!(
                "无法在窗口里打开应用页面 {url_text}。\n\
                 窗口当前地址：{actual}\n\
                 最后一次错误：{err}"
            ))
        }
    }
}

/// 最后兜底：用目标地址**新建**一个窗口，再关掉旧的占位窗口。
///
/// 新建窗口时 URL 是 `WebviewUrl::External`，webview 从创建那一刻就指向目标，
/// 完全不依赖运行时导航，因此能绕开 WebView2 上任何「拒绝运行时跳转」的情形。
fn rebuild_window(app: &AppHandle, url_text: &str) -> Result<(), String> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};

    let url: tauri::Url = url_text
        .parse()
        .map_err(|err| format!("解析地址失败：{err}"))?;

    let mut builder = WebviewWindowBuilder::new(app, "app", WebviewUrl::External(url))
        .title("AI Resume Generator")
        .inner_size(1280.0, 900.0)
        .min_inner_size(960.0, 640.0)
        .center();

    // 沿用占位窗口的位置与尺寸，视觉上不突兀
    if let Some(old) = app.get_webview_window("main") {
        if let Ok(size) = old.inner_size() {
            builder = builder.inner_size(f64::from(size.width), f64::from(size.height));
        }
        if let Ok(pos) = old.outer_position() {
            builder = builder.position(f64::from(pos.x), f64::from(pos.y));
        }
    }

    let window = builder
        .build()
        .map_err(|err| format!("创建应用窗口失败：{err}"))?;

    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if current_url(&window).starts_with(url_text) {
            if let Some(old) = app.get_webview_window("main") {
                let _ = old.close();
            }
            return Ok(());
        }
        thread::sleep(Duration::from_millis(150));
    }
    Err("新窗口创建后地址仍不是目标地址".to_string())
}

/// 启动后端并完成「就绪 → 校验 → 导航」全流程。
fn start_backend(app: AppHandle, window: WebviewWindow) {
    log_launch("==================== 应用启动 ====================");
    let ui = Ui::new(window);

    let data_dir = data_dir().to_path_buf();
    log_launch(&format!("start_backend: 数据目录 = {}", data_dir.display()));

    if let Err(err) = std::fs::create_dir_all(&data_dir) {
        ui.fail(
            "start_backend/create_dir_all",
            &format!(
                "无法创建数据目录：{}\n{err}\n\n\
                 请确认该目录可写，或用环境变量 APP_DATA_DIR 指定另一个目录。",
                data_dir.display()
            ),
        );
        return;
    }

    let port_file = data_dir.join("port.txt");
    // 删掉上一次的端口文件：否则兜底线程可能读到过期端口，去连一个「别人的服务」。
    match std::fs::remove_file(&port_file) {
        Ok(()) => log_launch("start_backend: 已清理旧的 port.txt"),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            log_launch("start_backend: 无旧 port.txt（正常）")
        }
        Err(err) => log_launch(&format!("start_backend: 清理旧 port.txt 失败（忽略）：{err}")),
    }

    let command = match app.shell().sidecar("resume-server") {
        Ok(command) => command,
        Err(err) => {
            ui.fail(
                "start_backend/sidecar_resolve",
                &format!(
                    "找不到后端程序 resume-server.exe。\n{err}\n\n\
                     安装包可能不完整，请重新安装。"
                ),
            );
            return;
        }
    };

    let command = command
        .args(["--port", "0"])
        .env("APP_DATA_DIR", data_dir.to_string_lossy().to_string())
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        // 本地回环请求必须绕过系统代理，否则健康检查会被代理拦截（502）
        .env("NO_PROXY", "localhost,127.0.0.1,::1")
        .env("no_proxy", "localhost,127.0.0.1,::1");

    let (mut receiver, child) = match command.spawn() {
        Ok(pair) => pair,
        Err(err) => {
            ui.fail(
                "start_backend/spawn",
                &format!(
                    "无法启动后端服务 resume-server.exe：\n{err}\n\n\
                     若杀毒软件弹过拦截提示，请选择「允许」。"
                ),
            );
            return;
        }
    };
    log_launch(&format!("start_backend: sidecar spawn 成功（pid={}）", child.pid()));

    if let Some(state) = app.try_state::<SidecarState>() {
        if let Ok(mut slot) = state.0.lock() {
            *slot = Some(child);
        }
    }

    // 看门狗：兜住「sidecar 既不报就绪也不退出」的挂起场景。
    {
        let ui = ui.clone();
        let log_path = data_dir.join("launch.log");
        thread::spawn(move || {
            let deadline = Instant::now() + STARTUP_TIMEOUT;
            while Instant::now() < deadline {
                if ui.is_ready() {
                    return;
                }
                thread::sleep(Duration::from_millis(200));
            }
            if !ui.is_ready() {
                ui.fail(
                    "watchdog",
                    &format!(
                        "后端在 {} 秒内没有完成启动。\n\
                         常见原因：杀毒软件拦截了 resume-server.exe、磁盘空间不足、\
                         或上次的进程未完全退出。\n\n\
                         诊断日志：{}",
                        STARTUP_TIMEOUT.as_secs(),
                        log_path.display()
                    ),
                );
            }
        });
    }

    // 事件泵：持续读取 sidecar 的 stdout/stderr（不能中途丢弃接收端，
    // 否则管道写满会让 sidecar 阻塞在 write 上）。
    // 这里 clone 一份给事件泵，`ui` 本体留给后面的端口文件兜底线程。
    let pump_ui = ui.clone();
    let pump_app = app.clone();
    let pump_log_path = data_dir.join("launch.log");
    thread::spawn(move || {
        let mut handled = false;
        loop {
            match receiver.blocking_recv() {
                Some(CommandEvent::Stdout(bytes)) => {
                    let text = String::from_utf8_lossy(&bytes);
                    let preview: String = text.chars().take(200).collect();
                    log_launch(&format!("stdout: {:?}", preview.trim_end()));
                    if handled {
                        continue;
                    }
                    let port = text.lines().find_map(parse_ready_line);
                    log_launch(&format!("parse_ready_line -> {:?}", port));
                    if let Some(port) = port {
                        handled = true;
                        spawn_health_check(pump_app.clone(), pump_ui.clone(), port, "stdout");
                    }
                }
                Some(CommandEvent::Stderr(bytes)) => {
                    let text = String::from_utf8_lossy(&bytes);
                    let preview: String = text.chars().take(300).collect();
                    log_launch(&format!("stderr: {:?}", preview.trim_end()));
                }
                Some(CommandEvent::Terminated(payload)) => {
                    log_launch(&format!("sidecar 已退出：code={:?}", payload.code));
                    if !handled && !pump_ui.is_ready() {
                        pump_ui.fail(
                            "sidecar/terminated",
                            &format!(
                                "后端进程意外退出（退出码 {:?}）。\n\
                                 请查看数据目录下的 launch.log，或重新安装。\n\n\
                                 日志位置：{}",
                                payload.code,
                                pump_log_path.display()
                            ),
                        );
                    }
                    break;
                }
                Some(_) => {}
                None => {
                    log_launch("stdout 通道已关闭");
                    if !handled && !pump_ui.is_ready() {
                        pump_ui.fail(
                            "sidecar/stdout_closed",
                            "后端进程已结束，且没有报告就绪端口。",
                        );
                    }
                    break;
                }
            }
        }
    });

    // 端口文件兜底：stdout 协议因编码/缓冲问题没解析出来时，仍能启动。
    {
        let ui = ui.clone();
        let fallback_app = app.clone();
        let port_file = port_file.clone();
        thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(20);
            while Instant::now() < deadline {
                if ui.is_ready() {
                    return;
                }
                if let Some(port) = read_port_file(&port_file) {
                    log_launch(&format!("port.txt 兜底读到的端口 = {port}"));
                    spawn_health_check(fallback_app.clone(), ui.clone(), port, "port-file");
                    return;
                }
                thread::sleep(Duration::from_millis(500));
            }
            log_launch("port.txt 兜底：超时未读到端口");
        });
    }
}

/// 拿到端口后：校验身份 → 导航。放在独立线程里跑（阻塞式 HTTP 轮询）。
fn spawn_health_check(app: AppHandle, ui: Ui, port: u16, source: &'static str) {
    thread::spawn(move || {
        if !ui.claim() {
            log_launch(&format!("health[{source}]: 已有线程在处理，跳过 port={port}"));
            return;
        }
        log_launch(&format!("health[{source}]: 开始校验 port={port}"));

        let client = match reqwest::blocking::Client::builder()
            // 必须禁用代理：系统若设了 http_proxy，127.0.0.1 会被代理拦截
            .no_proxy()
            .timeout(HEALTH_REQUEST_TIMEOUT)
            .build()
        {
            Ok(client) => client,
            Err(err) => {
                ui.fail(
                    "health/client_build",
                    &format!("无法创建健康检查客户端：{err}"),
                );
                return;
            }
        };

        if let Err(err) = wait_for_health(&client, port) {
            ui.fail("health/wait", &err);
            return;
        }
        log_launch("health: 校验通过，准备导航");

        // 先导航并校验，**成功之后**才 mark_ready（顺序反转会吞掉导航失败）。
        match navigate(&app, &ui.window, port) {
            Ok(how) => {
                log_launch(&format!("导航成功（{how}），窗口应已显示应用页面"));
                ui.mark_ready();
            }
            Err(err) => {
                ui.fail("navigate", &err);
            }
        }
    });
}

/// 退出时杀掉 sidecar（`CommandChild::kill` 内部会 TerminateProcess）。
fn stop_sidecar(app: &AppHandle) {
    if let Some(state) = app.try_state::<SidecarState>() {
        if let Ok(mut slot) = state.0.lock() {
            if let Some(child) = slot.take() {
                log_launch("stop_sidecar: 正在结束 sidecar");
                let _ = child.kill();
            }
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(SidecarState::default())
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                start_backend(app.handle().clone(), window);
            } else {
                log_launch("setup: 找不到 label=main 的窗口");
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Tauri 应用初始化失败")
        .run(|app_handle, event| match event {
            RunEvent::Exit => {
                stop_sidecar(app_handle);
            }
            RunEvent::ExitRequested { .. } => {
                stop_sidecar(app_handle);
            }
            _ => {}
        });
}
