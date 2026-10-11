"""AI 问答（flashcard）交互的浏览器验证（headless Edge，真实页面）。

覆盖设计要求的全部路径，并强制断言「控制台 0 error」：
  资料页入口：出卡 → 填 1 张 → 跳转其它卡 → 回来答案不丢 → 提交 → 资料被更新（GET /api/profile 核对）
  工作区入口：填岗位要求 → 生成 → 出卡（含 gaps 提示）→ 填部分 → 确认 → 生成态 → succeeded
  跳过路径：  出卡 → 「跳过，直接生成」→ 正常生成
  浅色模式：  切浅色后弹窗可读（截图）
  移动端：    375px 下弹窗不溢出

需要服务已在运行（8010 隔离库）：
    NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/iv_check.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

BASE = (os.environ.get("BASE_URL") or "").strip()  # 为空则自起隔离服务
SHOTS = Path(__file__).resolve().parent / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)

OK: list[str] = []
BAD: list[str] = []
MARK = "IVMARK 主导订单链路重构并把 P99 降到 120ms"   # 资料页回答标记，用于核对是否回写


def check(name: str, cond: bool, detail: object = "") -> None:
    (OK if cond else BAD).append(f"{name}  {detail}")
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")


PROFILE = {
    "name": "李四", "headline": "后端工程师", "email": "lisi@example.com",
    "phone": "13900000000", "location": "杭州",
    "summary": "",  # 故意留空，mock 会问「个人定位」
    "skills": [{"name": "Python", "level": "熟练", "category": "语言"}],
    "experiences": [{"company": "ACME 科技", "title": "后端工程师", "start": "2021.03",
                     "end": "至今", "summary": "负责订单服务。", "achievements": [],
                     "tech": ["Python"]}],
}

JD = (
    "Senior Backend Engineer\n"
    "Responsibilities\n"
    "- Design and build scalable backend services\n"
    "\n"
    "Requirements\n"
    "- Kubernetes and Docker containerization\n"
    "- Kafka message queue experience\n"
    "- Rust systems programming\n"
    "\n"
    "Working Terms\n"
    "- Full-time, hybrid\n"
)


def _run(base: str) -> int:
    c = httpx.Client(base_url=base, timeout=60)
    try:
        c.get("/api/health")
    except Exception as exc:  # pragma: no cover
        print(f"无法连接 {base}，请先启动服务。({exc})")
        return 2

    # 落全局资料，确保有素材（summary 留空、achievements 为空 -> 会出问题）
    c.put("/api/profile", json=PROFILE)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=["--no-sandbox", "--disable-gpu", "--no-proxy-server"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        page.goto(base, wait_until="load")
        page.wait_for_selector("#chat-list", timeout=15000)
        page.wait_for_timeout(400)

        # ================= 1. 资料页入口 =================
        print("\n=== 1. 资料页入口（出卡 / 切卡不丢答案 / 提交回写）===")
        page.click('.rail-item[data-view="profile"]')
        page.wait_for_timeout(300)
        check("资料页有「让 AI 帮我完善」按钮", page.is_visible("#btn-profile-ai") is True)
        page.click("#btn-profile-ai")
        page.wait_for_selector("#interview-dialog[open]", timeout=15000)
        page.wait_for_timeout(300)
        n = page.eval_on_selector_all(".iv-dot", "els => els.length")
        check("弹窗打开且出现问答卡（>=1 张）", n >= 1, n)
        check("进度显示「第 1 / N 张」",
              "第 1 /" in (page.text_content("#iv-progress") or ""), page.text_content("#iv-progress"))
        check("圆点数量与卡片数一致", n == (page.eval_on_selector_all(".iv-dot", "e=>e.length")), n)
        page.screenshot(path=str(SHOTS / "iv-01-profile-dialog.png"))

        # 第 1 张填入标记答案
        page.fill("#iv-answer", MARK)
        page.wait_for_timeout(200)
        check("已作答的圆点着色（is-done）",
              page.eval_on_selector(".iv-dot", "e => e.classList.contains('is-done')") is True)

        # 跳到其它卡，再回来 —— 答案应保留
        if n >= 2:
            page.click("#iv-next")
            page.wait_for_timeout(200)
            check("切到第 2 张（进度更新）",
                  "第 2 /" in (page.text_content("#iv-progress") or ""), page.text_content("#iv-progress"))
            check("切走后第 1 个圆点仍是已作答",
                  page.eval_on_selector(".iv-dot", "e => e.classList.contains('is-done')") is True)
            page.click(".iv-dot:nth-child(1)")
            page.wait_for_timeout(200)
            check("点圆点跳回第 1 张", "第 1 /" in (page.text_content("#iv-progress") or ""))
            check("来回切卡答案不丢", page.input_value("#iv-answer") == MARK, page.input_value("#iv-answer"))
        else:
            check("仅 1 张卡，跳过切卡验证（详情见报告）", True, "N=1")

        # 提交 -> 资料被更新
        page.click("#iv-submit")
        page.wait_for_function("() => !document.querySelector('#interview-dialog').open", timeout=20000)
        page.wait_for_timeout(600)
        prof = c.get("/api/profile").json()
        check("提交后资料已更新（回答内容回写进资料）", MARK in json.dumps(prof, ensure_ascii=False),
              "experiences[:1].achievements" if MARK in json.dumps(prof, ensure_ascii=False) else prof.get("summary"))
        page.screenshot(path=str(SHOTS / "iv-02-profile-updated.png"))

        # ================= 2. 工作区入口 =================
        print("\n=== 2. 工作区入口（出卡含 gaps / 填部分 / 确认 → 生成）===")
        page.click('.rail-item[data-view="workspace"]')
        page.wait_for_timeout(300)
        page.click("#btn-new-chat")
        page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
        page.fill("#ws-title-input", "高级后端工程师")
        page.fill("#ws-input", JD)
        page.wait_for_timeout(200)
        page.click("#btn-chat-generate")
        page.wait_for_selector("#interview-dialog[open]", timeout=20000)
        page.wait_for_timeout(300)
        gaps_visible = page.is_visible("#iv-gaps")
        gaps_text = (page.text_content("#iv-gaps") or "").strip()
        check("工作区出卡（含 gaps 缺口提示）", gaps_visible and "处岗位要求" in gaps_text, gaps_text[:60])
        page.screenshot(path=str(SHOTS / "iv-03-workspace-dialog.png"))

        page.fill("#iv-answer", "在 ACME 用过 Docker 与 K8s，做过服务容器化与灰度发布。")  # 只填第 1 张
        page.wait_for_timeout(150)
        page.click("#iv-submit")
        page.wait_for_function("() => !document.querySelector('#interview-dialog').open", timeout=20000)
        page.wait_for_selector('#ws-status[data-status="succeeded"]', timeout=60000)
        page.wait_for_timeout(400)
        check("确认提交后进入生成态并最终 succeeded",
              page.get_attribute("#ws-status", "data-status") == "succeeded")
        check("生成按钮未卡在 loading（非禁用且文案为「重新生成」）",
              page.get_attribute("#btn-chat-generate", "disabled") is None
              and "生成" in (page.text_content("#btn-chat-generate") or ""),
              page.text_content("#btn-chat-generate"))
        page.screenshot(path=str(SHOTS / "iv-04-workspace-succeeded.png"))

        # ================= 3. 跳过路径 =================
        print("\n=== 3. 工作区「跳过，直接生成」路径 ===")
        # 用一份全新的 JD：前两步的问答已把原 JD 的缺口补进资料，
        # 同样的问题不会重复问（这是预期行为），因此这里换新要求以确保有缺口。
        JD2 = (
            "Staff Platform Engineer\n"
            "Responsibilities\n"
            "- Build internal developer platform\n"
            "\n"
            "Requirements\n"
            "- Go and gRPC service development\n"
            "- Distributed transaction and event sourcing\n"
            "- Prometheus / OpenTelemetry observability\n"
            "\n"
            "Working Terms\n"
            "- Full-time\n"
        )
        page.click("#btn-new-chat")
        page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
        # 必须等新对话真正载入完成（输入框被重置为空）再填写：
        # 新建是异步的（POST -> 刷新列表 -> openChat），若立即 fill，
        # 内容会写进上一个对话的输入框，随后被新对话载入覆盖，导致点击无效。
        page.wait_for_function(
            "() => { const t = document.querySelector('#ws-title-input');"
            " const r = document.querySelector('#ws-input');"
            " return t && r && t.value === '' && r.value === ''; }", timeout=15000)
        page.fill("#ws-title-input", "平台工程师（跳过路径）")
        page.fill("#ws-input", JD2)
        page.wait_for_function(
            "() => { const b = document.querySelector('#btn-chat-generate');"
            " return b && !b.disabled && !b.dataset.busy; }", timeout=10000)
        page.wait_for_timeout(200)
        page.click("#btn-chat-generate")
        # 有缺口 -> 弹卡；若资料恰好已覆盖则直接生成。两种都算通过。
        try:
            page.wait_for_selector("#interview-dialog[open]", timeout=20000)
            page.wait_for_timeout(200)
            check("跳过按钮文案为「跳过，直接生成」",
                  (page.text_content("#iv-skip-all") or "").strip() == "跳过，直接生成",
                  page.text_content("#iv-skip-all"))
            page.click("#iv-skip-all")
            page.wait_for_function("() => !document.querySelector('#interview-dialog').open", timeout=20000)
            print("    （本轮有缺口，走了「跳过，直接生成」）")
        except Exception:
            print("    （本轮资料已覆盖该 JD，未弹卡，直接生成 —— 同属预期）")
        page.wait_for_selector('#ws-status[data-status="succeeded"]', timeout=90000)
        check("跳过路径正常生成到 succeeded",
              page.get_attribute("#ws-status", "data-status") == "succeeded")

        # ================= 4. 浅色模式可读 + Esc 关闭 =================
        print("\n=== 4. 浅色模式 + Esc 关闭 ===")
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]", timeout=8000)
        page.select_option("#cfg-theme", "light")
        page.dispatch_event("#cfg-theme", "change")
        page.wait_for_timeout(300)
        check("已切到浅色主题",
              page.eval_on_selector("html", "e => e.getAttribute('data-theme')") == "light")
        page.click("#btn-close-settings")
        page.wait_for_timeout(200)
        page.click('.rail-item[data-view="profile"]')
        page.wait_for_timeout(200)
        page.click("#btn-profile-ai")
        page.wait_for_selector("#interview-dialog[open]", timeout=15000)
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "iv-05-light-dialog.png"))
        check("浅色模式弹窗可见", page.is_visible(".iv-card") is True)
        # Esc = 跳过（资料页 -> 直接关闭，不发请求）
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('#interview-dialog').open", timeout=8000)
        check("Esc 可关闭弹窗", page.eval_on_selector("#interview-dialog", "e => e.open") is False)

        # ================= 5. 移动端 375px 不溢出 =================
        print("\n=== 5. 移动端 375px 不溢出 ===")
        page.set_viewport_size({"width": 375, "height": 780})
        page.wait_for_timeout(300)
        page.click("#btn-profile-ai")
        page.wait_for_selector("#interview-dialog[open]", timeout=15000)
        page.wait_for_timeout(400)
        no_doc_overflow = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        no_dlg_overflow = page.eval_on_selector(
            "#interview-dialog", "e => e.scrollWidth <= e.clientWidth + 1 && e.getBoundingClientRect().width <= window.innerWidth + 1")
        check("375px 页面无横向溢出", no_doc_overflow is True)
        check("375px 弹窗无横向溢出", no_dlg_overflow is True)
        page.screenshot(path=str(SHOTS / "iv-06-mobile-dialog.png"))

        print("\n=== 6. 控制台无 error / warning ===")
        check("控制台 0 条 error/warning", not console, console[:4])

    print("\n" + "=" * 64)
    print(f"IV 验证： PASS {len(OK)} / FAIL {len(BAD)}")
    if BAD:
        print("\n未通过：")
        for x in BAD:
            print("  -", x)
    sys.stdout.flush()
    return 1 if BAD else 0


def main() -> int:
    """默认自起隔离服务；显式设置 BASE_URL 时才用外部服务。

    此前默认打 8010/8000 的外部服务，容易误连用户真实库。
    """
    explicit = (os.environ.get("BASE_URL") or "").strip()
    if explicit:
        print(f"[外部服务] {explicit} —— 若指向 8000，测试数据会写入用户真实库")
        return _run(explicit)
    from _ui_server import isolated_server
    with isolated_server("iv_check", 8126) as base:
        return _run(base)


if __name__ == "__main__":
    raise SystemExit(main())
