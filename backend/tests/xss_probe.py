"""XSS 端到端验证：无头 Edge 加载 /preview，断言注入脚本不执行。

自起一个**隔离 DB、独立端口（8123）**的 uvicorn 子进程，绝不触碰 8000 端口进程。
验证两层防御：
  A) ai-edit 路径：nh3 入库前净化 -> /preview 无 alert
  B) PUT 路径（模拟净化被绕过）：直接写入原始 payload -> CSP 兜底 -> /preview 仍无 alert

运行： ./.venv/Scripts/python.exe backend/tests/xss_probe.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent  # 项目根目录
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
DB_FILE = ROOT / "data" / "xss_probe.db"

PROFILE = {
    "name": "张三",
    "headline": "5 年后端工程师",
    "summary": "专注高并发后端。",
    "skills": [{"name": "Python", "level": "精通", "category": "语言"}],
    "experiences": [{"company": "ACME", "title": "工程师", "start": "2021", "end": "至今",
                     "summary": "重构订单链路。", "achievements": ["P99 800ms→120ms"]}],
    "education": [{"school": "某某大学", "degree": "本科", "major": "计算机", "start": "2014", "end": "2018"}],
}

JOB = "高级后端工程师\n- 3 年以上 Python 后端开发经验\n- 熟悉 Kubernetes"

# 5 条 QA 实测绕过 + 1 条点击型；都尝试改写标题/弹窗，便于断言是否执行
PAYLOADS = [
    '<img/onerror="document.title=\'PWNED1\';alert(1)" src=x>',
    '<svg/onload="document.title=\'PWNED2\';alert(2)"></svg>',
    '<svg/onload=alert(3)>',
    '<a href="jav&#x61;script:alert(4)">a</a>',
    '<a href="&#106;avascript:alert(5)">b</a>',
    '<div/onclick="alert(6)">c</div>',
]

_PASS = 0
_FAIL = 0


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
            print("服务器未就绪 ❌")
            return 1

        client = httpx.Client(base_url=BASE, timeout=30)
        client.put("/api/profile", json=PROFILE)
        tid = client.post("/api/tasks", json={"title": "XSS 探针", "raw_text": JOB}).json()["id"]
        deadline = time.time() + 40
        while time.time() < deadline:
            t = client.get(f"/api/tasks/{tid}").json()
            if t["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.3)
        assert t["status"] == "succeeded", t
        rid = t["resume_id"]
        base_html = client.get(f"/api/resumes/{rid}").json()["html"]
        expected_title = client.get(f"/api/resumes/{rid}").json()["title"]

        # ---- A) ai-edit 路径（nh3 净化）----
        inject = base_html + "".join(PAYLOADS)
        edited = client.post(f"/api/resumes/{rid}/ai-edit",
                             json={"instruction": "保持原样", "current_html": inject}).json()["html"]
        print("[A] ai-edit 后存储内容检查")
        check("存储内容不含 onerror", "onerror" not in edited.lower(), edited[:80])
        check("存储内容不含 <svg", "<svg" not in edited.lower())
        check("存储内容不含 javascript:", "javascript:" not in edited.lower())

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)

            # A) 预览 ai-edit 后的简历：无 alert + 简历仍正常渲染
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            dialogs: list[str] = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.goto(f"{BASE}/api/resumes/{rid}/preview", wait_until="load")
            page.wait_for_timeout(600)
            print("\n[A] 无头 Edge 加载 /preview（ai-edit 产物）")
            check("无 alert 弹出", not dialogs, dialogs)
            check("标题未被脚本改写", page.title() != "PWNED1" and page.title() != "PWNED2", page.title())
            height = page.eval_on_selector(".resume", "el => el.getBoundingClientRect().height")
            weight = page.eval_on_selector(".r-name", "el => getComputedStyle(el).fontWeight")
            check("简历仍正常渲染（.resume 高度 > 200）", height and height > 200, height)
            check("内联样式生效（.r-name font-weight=800）", weight == "800", weight)
            page.close()

            # B) PUT 原始 payload（模拟净化被绕过）-> CSP 兜底
            client.put(f"/api/resumes/{rid}", json={"html": base_html + "".join(PAYLOADS)})
            prev_b = client.get(f"/api/resumes/{rid}/preview").text
            check("CSP 存在且 default-src 'none'",
                  "Content-Security-Policy" in prev_b and "default-src 'none'" in prev_b)
            check("原始 payload 确实进入了 preview（证明测的是 CSP）", "onerror" in prev_b.lower())

            page2 = browser.new_page(viewport={"width": 1200, "height": 900})
            dialogs2: list[str] = []
            page2.on("dialog", lambda d: (dialogs2.append(d.message), d.dismiss()))
            page2.goto(f"{BASE}/api/resumes/{rid}/preview", wait_until="load")
            page2.wait_for_timeout(800)
            print("\n[B] 无头 Edge 加载 /preview（绕过净化 + CSP 兜底）")
            check("CSP 拦截：无 alert 弹出", not dialogs2, dialogs2)
            check("CSP 拦截：标题未被改写",
                  page2.title() != "PWNED1" and page2.title() != "PWNED2", page2.title())
            check("期望标题一致", page2.title() == expected_title, (page2.title(), expected_title))
            page2.close()
            browser.close()

        client.close()
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

    print(f"\nXSS 探针结果：{_PASS} 通过 / {_FAIL} 失败")
    if _FAIL:
        print("存在失败用例 ❌")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
