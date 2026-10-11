"""供应商预设模型配置界面的前端实测（无头 Edge，隔离服务 + 隔离 DB）。

自起一个 **隔离 DB、独立端口（8124）** 的 uvicorn 子进程，绝不触碰 8000 端口进程，
也不读写用户真实配置。验证：
  1. 供应商下拉框：DeepSeek 排第一且默认选中；Base URL 只读自动填充。
  2. 切换供应商 -> Base URL 自动填充；选 custom -> Base URL 变为可编辑。
  3. 模型下拉框有预设值，且含「＋ 自定义模型名…」。
  4. 「测试连接」各分支在**本地可控**的地址下给出可读中文提示（不真的打 DeepSeek）。
  5. 保存后顶栏与页脚文案反映「供应商 · 模型 · worker」。

运行： ./.venv/Scripts/python.exe backend/tests/settings_ui_check.py

若运行环境禁止 Playwright 自行拉起浏览器进程，可先手动启动 Edge 并改用 CDP 连接：
    msedge.exe --headless=new --remote-debugging-port=9333 --user-data-dir=/tmp/edgeprof about:blank
    PLAYWRIGHT_CDP_URL=http://127.0.0.1:9333 ./.venv/Scripts/python.exe backend/tests/settings_ui_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent  # 项目根目录
PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"
DB_FILE = ROOT / "data" / "settings_ui.db"
OUT = Path(__file__).resolve().parent / "shots"
OUT.mkdir(exist_ok=True)

_PASS = 0
_FAIL = 0
_LOGS: list[str] = []


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  \u2713 {name}")
    else:
        _FAIL += 1
        print(f"  \u2717 {name}  -> {extra!r}")


def _wait_server(timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/api/health", timeout=1.5).status_code == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def _wait_result(page, needle: str, timeout: int = 25000) -> str:
    """等待 #cfg-test-result 出现 needle，返回其文本。"""
    try:
        page.wait_for_function(
            "([sel, needle]) => { const el = document.querySelector(sel);"
            " return !!el && el.textContent.includes(needle); }",
            arg=["#cfg-test-result", needle],
            timeout=timeout,
        )
    except Exception:
        pass
    return page.inner_text("#cfg-test-result")


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(DB_FILE) + suffix)
        if f.exists():
            f.unlink()

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
    env["PORT"] = str(PORT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_server():
            print("隔离服务器未就绪 ❌")
            return 1
        print(f"隔离服务器就绪：{BASE}（DB={DB_FILE.name}）")

        with sync_playwright() as p:
            cdp_url = os.environ.get("PLAYWRIGHT_CDP_URL", "").strip()
            if cdp_url:
                # 连接到外部已启动的 Edge（用于禁止 Playwright 拉起浏览器的环境）
                browser = p.chromium.connect_over_cdp(cdp_url)
                page = browser.contexts[0].new_page()
                page.set_viewport_size({"width": 1440, "height": 1000})
                print(f"已通过 CDP 连接浏览器：{cdp_url}")
            else:
                browser = p.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("console", lambda m: _LOGS.append(f"[console.{m.type}] {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: _LOGS.append(f"[pageerror] {e}"))

            page.goto(BASE, wait_until="networkidle")
            page.wait_for_timeout(600)

            # ---------- 打开设置弹窗 ----------
            page.click("#btn-settings")
            page.wait_for_timeout(500)

            opts = page.eval_on_selector_all("#cfg-provider option", "els => els.map(e => e.value)")
            check("供应商下拉框第一项是 deepseek", bool(opts) and opts[0] == "deepseek", opts[:3])
            check("默认选中 deepseek", page.input_value("#cfg-provider") == "deepseek",
                  page.input_value("#cfg-provider"))
            check("Base URL 自动填充为 DeepSeek 预设",
                  page.input_value("#cfg-base") == "https://api.deepseek.com", page.input_value("#cfg-base"))
            check("非自定义供应商 Base URL 只读",
                  page.get_attribute("#cfg-base", "readonly") is not None)
            check("文档链接指向 DeepSeek 文档",
                  "api-docs.deepseek.com" in (page.get_attribute("#cfg-docs", "href") or ""),
                  page.get_attribute("#cfg-docs", "href"))
            models = page.eval_on_selector_all("#cfg-model-select option", "els => els.map(e => e.value)")
            check("模型下拉框含 deepseek-flash", "deepseek-flash" in models, models)
            check("模型下拉框含 deepseek-v4-pro", "deepseek-v4-pro" in models, models)
            check("模型下拉框含「自定义模型名」项", "__custom__" in models, models)
            check("模型默认选中 deepseek-flash", page.input_value("#cfg-model-select") == "deepseek-flash",
                  page.input_value("#cfg-model-select"))
            check("默认隐藏自定义模型名输入框",
                  page.eval_on_selector("#cfg-model-custom-field", "el => getComputedStyle(el).display") == "none")
            # 选「＋ 自定义模型名…」-> 显示手填输入框
            page.select_option("#cfg-model-select", "__custom__")
            page.wait_for_timeout(200)
            check("选自定义模型名 -> 显示手填输入框",
                  page.eval_on_selector("#cfg-model-custom-field", "el => getComputedStyle(el).display") != "none")
            page.fill("#cfg-model-custom", "my-custom-model")
            page.select_option("#cfg-model-select", "deepseek-flash")
            page.wait_for_timeout(200)
            check("切回预设模型 -> 再次隐藏手填输入框",
                  page.eval_on_selector("#cfg-model-custom-field", "el => getComputedStyle(el).display") == "none")
            page.screenshot(path=str(OUT / "20-settings-deepseek.png"))
            print("[shot] 20 设置弹窗（DeepSeek 默认）")

            # ---------- 切换供应商 ----------
            page.select_option("#cfg-provider", "openai")
            page.wait_for_timeout(300)
            check("切到 OpenAI -> Base URL 自动填充",
                  page.input_value("#cfg-base") == "https://api.openai.com/v1", page.input_value("#cfg-base"))
            check("切到 OpenAI -> 模型下拉框有值",
                  page.input_value("#cfg-model-select") not in ("", None), page.input_value("#cfg-model-select"))

            page.select_option("#cfg-provider", "ollama")
            page.wait_for_timeout(300)
            check("切到 Ollama -> Base URL 为本地地址",
                  page.input_value("#cfg-base") == "http://localhost:11434/v1", page.input_value("#cfg-base"))

            page.select_option("#cfg-provider", "custom")
            page.wait_for_timeout(300)
            check("切到 custom -> Base URL 变为可编辑",
                  page.get_attribute("#cfg-base", "readonly") is None)
            page.screenshot(path=str(OUT / "21-settings-custom.png"))
            print("[shot] 21 自定义供应商（Base URL 可编辑）")

            # ---------- 测试连接：本地可控分支 ----------
            # 1) 空 key
            page.fill("#cfg-base", f"{BASE}/nope")
            page.fill("#cfg-key", "")
            page.click("#btn-test")
            txt = _wait_result(page, "API Key")
            check("空 key -> 提示填写 API Key", "API Key" in txt, txt)

            # 2) 404（/models 与回退 chat 均 404）-> Base URL 提示
            page.fill("#cfg-key", "sk-test-123")
            page.click("#btn-test")
            txt = _wait_result(page, "Base URL")
            check("404 -> 提示检查 Base URL", "Base URL" in txt, txt)
            page.screenshot(path=str(OUT / "22-settings-test-404.png"))
            print("[shot] 22 测试连接（404 分支）")

            # 3) 不可达地址 -> 可读失败提示（本地端口，不触碰外部网络）
            #    注：不同环境下底层异常可能是 ConnectError / 超时 / 代理 502，
            #    这里断言的是"给出可读的中文失败结论"，而非具体措辞。
            page.fill("#cfg-base", "http://127.0.0.1:9/v1")
            page.click("#btn-test")
            page.wait_for_function(
                "() => { const el = document.querySelector('#cfg-test-result');"
                " return !!el && el.textContent.trim() !== '' && !el.textContent.includes('正在测试'); }",
                timeout=30000,
            )
            txt = page.inner_text("#cfg-test-result")
            readable = bool(txt.strip()) and ("连接成功" not in txt) and any(
                k in txt for k in ("无法连接", "超时", "Base URL", "服务端错误", "网络"))
            check("不可达地址 -> 可读失败提示", readable, txt)
            page.screenshot(path=str(OUT / "23-settings-test-conn.png"))
            print("[shot] 23 测试连接（连接异常分支）")

            # ---------- 保存并检查顶栏 ----------
            page.select_option("#cfg-provider", "deepseek")
            page.wait_for_timeout(300)
            check("切回 DeepSeek -> Base URL 恢复预设",
                  page.input_value("#cfg-base") == "https://api.deepseek.com", page.input_value("#cfg-base"))
            check("切回 DeepSeek -> 模型恢复预设", page.input_value("#cfg-model-select") == "deepseek-flash",
                  page.input_value("#cfg-model-select"))
            page.fill("#cfg-key", "sk-fake-ui-check")
            page.click("#btn-save-settings")
            page.wait_for_timeout(1200)

            # 顶栏 / 页脚经 CSS text-transform: uppercase 渲染，比较时统一小写
            top = page.inner_text("#llm-status")
            foot = page.inner_text("#footer-mode")
            top_l, foot_l = top.lower(), foot.lower()
            check("顶栏含供应商 DeepSeek", "deepseek" in top_l, top)
            check("顶栏含模型 deepseek-flash", "deepseek-flash" in top_l, top)
            check("页脚含 供应商 · 模型", "deepseek" in foot_l and "deepseek-flash" in foot_l, foot)
            print(f"    顶栏文案：{top!r}")
            print(f"    页脚文案：{foot!r}")
            page.screenshot(path=str(OUT / "24-settings-saved.png"))
            print("[shot] 24 保存后顶栏更新")

            # ---------- 重开弹窗：Key 状态条 + 脱敏回填 ----------
            page.click("#btn-settings")
            page.wait_for_timeout(500)
            status_txt = page.inner_text("#cfg-key-status") or ""
            check("重开弹窗显示「API Key 已配置」状态条", "已配置" in status_txt, status_txt)
            check("状态条不泄露完整 Key", "sk-fake-ui-check" not in status_txt, status_txt)
            ph = page.get_attribute("#cfg-key", "placeholder") or ""
            check("重开弹窗 Key 显示脱敏提示", "已配置" in ph and "sk-fake-ui-check" not in ph, ph)
            check("重开弹窗不回填真实 Key", page.input_value("#cfg-key") == "", page.input_value("#cfg-key"))
            check("重开弹窗供应商仍为 deepseek", page.input_value("#cfg-provider") == "deepseek",
                  page.input_value("#cfg-provider"))
            check("重开弹窗模型回填 deepseek-flash",
                  page.input_value("#cfg-model-select") == "deepseek-flash", page.input_value("#cfg-model-select"))
            check("设置弹窗已移除温度输入", page.locator("#cfg-temp").count() == 0)
            check("设置弹窗已移除 worker 输入", page.locator("#cfg-workers").count() == 0)
            page.screenshot(path=str(OUT / "25-settings-reopen.png"))
            print("[shot] 25 重开弹窗（Key 状态条 + 脱敏回填）")

            # 注：本机 headless Edge 偶发在 browser.close() 处挂起 driver 连接，
            # 该现象与页面逻辑无关，故交由 with 上下文在退出时统一回收浏览器。

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(DB_FILE) + suffix)
            if f.exists():
                f.unlink()

    if _LOGS:
        print("\n⚠️  浏览器控制台错误：")
        for e in _LOGS[:20]:
            print("   ", e)
    print(f"\n前端实测结果：{_PASS} 通过 / {_FAIL} 失败")
    if _FAIL or _LOGS:
        print("存在失败用例 ❌")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
