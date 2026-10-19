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

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

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
    ready: Arc<AtomicBool>,
    failed: Arc<AtomicBool>,
}

impl Ui {
    fn new(window: WebviewWindow) -> Self {
        Self {
            window,
            ready: Arc::new(AtomicBool::new(false)),
            failed: Arc::new(AtomicBool::new(false)),
        }
    }

    /// 是否已经成功导航到真实应用页面。
    fn is_ready(&self) -> bool {
        self.ready.load(Ordering::SeqCst)
    }

    /// 标记成功（只生效一次）。
    fn mark_ready(&self) -> bool {
        !self.failed.load(Ordering::SeqCst) && !self.ready.swap(true, Ordering::SeqCst)
    }

    /// 把占位页替换成可读错误；幂等，且就绪后不再干预页面。
    fn fail(&self, message: &str) {
        if self.is_ready() {
            return;
        }
        if self.failed.swap(true, Ordering::SeqCst) {
            return;
        }

        let window = self.window.clone();
        let payload = serde_json::to_string(message)
            .unwrap_or_else(|_| "\"后端启动失败（错误信息无法序列化）\"".to_string());
        // 占位页可能在窗口完成首帧前就被判失败，故稍等再注入，并重试一次。
        thread::spawn(move || {
            for attempt in 0..3 {
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
  }} catch (e) {{ /* 页面尚未就绪时忽略，等待下一次重试 */ }}
}})();"#,
                    payload = payload
                );
                if window.eval(&script).is_ok() && attempt >= 1 {
                    break;
                }
            }
        });
    }
}

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
        Err(err) => return Probe::Pending(format!("连接失败：{err}")),
    };

    if !response.status().is_success() {
        // 我们的 /api/health 恒返回 200；非 2xx 说明对面可能是别的服务，
        // 也可能是本应用尚未挂载完路由，继续轮询。
        return Probe::Pending(format!("HTTP {}", response.status().as_u16()));
    }

    match response.json::<HealthPayload>() {
        Ok(payload) => {
            if payload.app == APP_ID && payload.ok {
                Probe::Ours
            } else if payload.app.is_empty() {
                Probe::Pending("应答缺少 app 字段".to_string())
            } else {
                Probe::NotOurs(payload.app)
            }
        }
        // 200 但不是我们的 JSON —— 端口上确实是别的服务
        Err(err) => Probe::NotOurs(format!("非预期应答（{err}）")),
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

/// 把窗口导航到真实应用地址。
fn navigate(window: &WebviewWindow, port: u16) -> Result<(), String> {
    let url_text = format!("http://127.0.0.1:{port}/");
    let url: tauri::Url = url_text
        .parse()
        .map_err(|err| format!("无法解析应用地址 {url_text}：{err}"))?;
    window
        .navigate(url)
        .map_err(|err| format!("导航到 {url_text} 失败：{err}"))
}

/// 启动后端并完成「就绪 → 校验 → 导航」全流程。
fn start_backend(app: AppHandle, window: WebviewWindow) {
    let ui = Ui::new(window);

    let data_dir = resolve_data_dir();
    if let Err(err) = std::fs::create_dir_all(&data_dir) {
        ui.fail(&format!(
            "无法创建数据目录：{}\n{err}\n\n\
             请确认该目录可写，或用环境变量 APP_DATA_DIR 指定另一个目录。",
            data_dir.display()
        ));
        return;
    }
    let port_file = data_dir.join("port.txt");
    let _ = std::fs::remove_file(&port_file);

    let command = match app.shell().sidecar("resume-server") {
        Ok(command) => command,
        Err(err) => {
            ui.fail(&format!(
                "找不到后端程序 resume-server.exe。\n{err}\n\n\
                 安装包可能不完整，请重新安装。"
            ));
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
            ui.fail(&format!("无法启动后端服务 resume-server.exe：\n{err}"));
            return;
        }
    };

    if let Some(state) = app.try_state::<SidecarState>() {
        if let Ok(mut slot) = state.0.lock() {
            *slot = Some(child);
        }
    }

    // 看门狗：兜住「sidecar 既不报就绪也不退出」的挂起场景。
    {
        let ui = ui.clone();
        thread::spawn(move || {
            let deadline = Instant::now() + STARTUP_TIMEOUT;
            while Instant::now() < deadline {
                if ui.is_ready() {
                    return;
                }
                thread::sleep(Duration::from_millis(200));
            }
            if !ui.is_ready() {
                ui.fail(&format!(
                    "后端在 {} 秒内没有完成启动。\n\
                     常见原因：杀毒软件拦截了 resume-server.exe、磁盘空间不足、\
                     或上次的进程未完全退出。",
                    STARTUP_TIMEOUT.as_secs()
                ));
            }
        });
    }

    // 事件泵：持续读取 sidecar 的 stdout/stderr（不能中途丢弃接收端，
    // 否则管道写满会让 sidecar 阻塞在 write 上）。
    // 这里 clone 一份给事件泵，`ui` 本体留给后面的端口文件兜底线程。
    let pump_ui = ui.clone();
    thread::spawn(move || {
        let mut handled = false;
        loop {
            match receiver.blocking_recv() {
                Some(CommandEvent::Stdout(bytes)) => {
                    if handled {
                        continue;
                    }
                    let text = String::from_utf8_lossy(&bytes);
                    let port = text.lines().find_map(parse_ready_line);
                    if let Some(port) = port {
                        handled = true;
                        spawn_health_check(pump_ui.clone(), port);
                    }
                }
                Some(CommandEvent::Stderr(bytes)) => {
                    eprint!("{}", String::from_utf8_lossy(&bytes));
                }
                Some(CommandEvent::Terminated(payload)) => {
                    if !handled {
                        pump_ui.fail(&format!(
                            "后端进程意外退出（退出码 {:?}）。\n\
                             请查看安装目录下的日志，或重新安装。",
                            payload.code
                        ));
                    }
                    break;
                }
                Some(_) => {}
                None => {
                    if !handled {
                        pump_ui.fail("后端进程已结束，且没有报告就绪端口。");
                    }
                    break;
                }
            }
        }
    });

    // 端口文件兜底：stdout 协议因编码/缓冲问题没解析出来时，仍能启动。
    {
        let ui = ui.clone();
        thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(20);
            while Instant::now() < deadline {
                if ui.is_ready() {
                    return;
                }
                if let Some(port) = read_port_file(&port_file) {
                    if !ui.is_ready() && !ui.failed.load(Ordering::SeqCst) {
                        spawn_health_check(ui.clone(), port);
                    }
                    return;
                }
                thread::sleep(Duration::from_millis(500));
            }
        });
    }
}

/// 拿到端口后：校验身份 → 导航。放在独立线程里跑（阻塞式 HTTP 轮询）。
fn spawn_health_check(ui: Ui, port: u16) {
    thread::spawn(move || {
        let client = match reqwest::blocking::Client::builder()
            // 必须禁用代理：系统若设了 http_proxy，127.0.0.1 会被代理拦截
            .no_proxy()
            .timeout(HEALTH_REQUEST_TIMEOUT)
            .build()
        {
            Ok(client) => client,
            Err(err) => {
                ui.fail(&format!("无法创建健康检查客户端：{err}"));
                return;
            }
        };

        if let Err(err) = wait_for_health(&client, port) {
            ui.fail(&err);
            return;
        }

        if !ui.mark_ready() {
            return;
        }

        if let Err(err) = navigate(&ui.window, port) {
            ui.fail(&err);
        }
    });
}

/// 退出时杀掉 sidecar（`CommandChild::kill` 内部会 TerminateProcess）。
fn stop_sidecar(app: &AppHandle) {
    if let Some(state) = app.try_state::<SidecarState>() {
        if let Ok(mut slot) = state.0.lock() {
            if let Some(child) = slot.take() {
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
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Tauri 应用初始化失败")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                stop_sidecar(app_handle);
            }
        });
}
