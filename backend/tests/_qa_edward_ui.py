"""QA 二轮独立 UI 回归：空库默认进 01 + 完整踩坑序列（commit f71a336 前端部分）。

自起隔离服务（端口 8156，隔离库 _ui_qa_edward_ui.db），headless Edge。
运行： ./.venv/Scripts/python.exe backend/tests/_qa_edward_ui.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _ui_server import isolated_server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  -> {extra!r}")


def main() -> int:
    with isolated_server("qa_edward_ui", 8156) as base:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            console: list[str] = []
            page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

            # ---------------- B1 空库首屏 ----------------
            print("\n[B1] 空库首屏 -> 默认进 01（profile）")
            page.goto(base, wait_until="load")
            page.wait_for_selector(".view", state="attached", timeout=15000)
            page.wait_for_timeout(500)
            active = page.eval_on_selector_all(
                ".view", "els => els.filter(e => e.classList.contains('is-active'))"
                ".map(e => e.dataset.view)")
            check("激活视图为 profile（01）", active == ["profile"], active)
            rail_active = page.eval_on_selector_all(
                ".rail-item", "els => els.filter(e => e.classList.contains('is-active'))"
                ".map(e => e.dataset.view)")
            check("rail 01（profile）高亮", rail_active == ["profile"], rail_active)

            # ---------------- B3 准备：踩坑序列的「点 rail 去 02 -> 新建对话」 ----------------
            print("\n[B3] 完整踩坑序列（先做前半段，B2 再 reload）")
            page.click('.rail-item[data-view="workspace"]')
            page.wait_for_timeout(300)
            active = page.eval_on_selector_all(
                ".view", "els => els.filter(e => e.classList.contains('is-active'))"
                ".map(e => e.dataset.view)")
            check("点 rail 后切到 workspace（02）", active == ["workspace"], active)
            page.click("#btn-new-chat")
            page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
            page.wait_for_timeout(300)
            check("新对话面板已显示", page.is_visible("#ws-work") is True)
            check("快照栏姓名为空（当时全局为空，仅显示占位符）",
                  "（未填写姓名）" in (page.text_content("#ws-profile-name") or ""),
                  page.text_content("#ws-profile-name"))

            # 回 01 填 name + 一段经历，保存
            page.click('.rail-item[data-view="profile"]')
            page.wait_for_timeout(300)
            page.fill("#pf-name", "李四")
            page.click('[data-add="experiences"]')
            page.wait_for_timeout(200)
            exp = page.query_selector('#rep-experiences .rep-item')
            check("经历条目已添加", exp is not None)
            if exp:
                exp.query_selector('[data-field="company"]').fill("示例集团")
                exp.query_selector('[data-field="title"]').fill("测试开发工程师")
            page.click("#btn-save-profile")
            # 注意：新建对话可能残留旧 toast，必须精确等「保存」那条出现
            page.wait_for_selector(".toast:has-text('资料已保存，可去 02 生成简历')", timeout=8000)
            toast = page.text_content(".toast:has-text('资料已保存，可去 02 生成简历')") or ""
            check("保存 toast 文案正确", "资料已保存，可去 02 生成简历" in toast, toast)
            page.wait_for_timeout(400)

            # ---------------- B2 reload 后默认进 workspace ----------------
            print("\n[B2] 资料已填 -> reload -> 默认进 workspace（02）")
            page.reload(wait_until="load")
            page.wait_for_selector(".view", state="attached", timeout=15000)
            page.wait_for_timeout(500)
            active = page.eval_on_selector_all(
                ".view", "els => els.filter(e => e.classList.contains('is-active'))"
                ".map(e => e.dataset.view)")
            check("reload 后默认视图为 workspace（02）", active == ["workspace"], active)

            # ---------------- B3 后半段：回 02 填 JD -> 生成 ----------------
            print("\n[B3] 后半段：回 02 点开既有对话 -> 填 JD -> 生成 -> 全程无「尚未填写资料库」")
            page.click('.rail-item[data-view="workspace"]')
            # reload 后工作区显示会话列表，用户需点开刚才新建的对话
            page.wait_for_selector("#chat-list .chat-item", timeout=15000)
            page.click("#chat-list .chat-item")
            page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
            page.wait_for_timeout(300)
            check("快照栏仍显示占位符（对话快照尚未同步，符合快照语义）",
                  "（未填写姓名）" in (page.text_content("#ws-profile-name") or ""),
                  page.text_content("#ws-profile-name"))
            page.fill("#ws-title-input", "测试开发工程师")
            page.fill("#ws-input",
                      "测试开发工程师\nRequirements\n- 2 年以上 Python 经验\n"
                      "Responsibilities\n- 搭建自动化回归流水线")
            page.wait_for_timeout(200)
            page.click("#btn-chat-generate")
            try:
                page.wait_for_selector("#interview-dialog[open]", timeout=6000)
                page.click("#iv-skip-all")
            except Exception:
                pass  # 资料足够时不弹问答
            page.wait_for_selector('#ws-status[data-status="succeeded"]', timeout=45000)
            page.wait_for_timeout(400)
            check("生成 succeeded（mock 模式）",
                  page.get_attribute("#ws-status", "data-status") == "succeeded")
            body_text = page.text_content("body") or ""
            check("全程未出现「尚未填写资料库」", "尚未填写资料库" not in body_text)
            check("消息流出现 snapshot_sync 说明文案",
                  "已自动采用最新的资料库内容" in body_text)
            check("控制台 0 error", len(console) == 0, console[:5])
            page.screenshot(path=str(Path(__file__).resolve().parent / "shots" / "qa_edward_b3.png"))

            browser.close()

    print(f"\n===== QA 独立 UI 回归：PASS={PASS} FAIL={FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
