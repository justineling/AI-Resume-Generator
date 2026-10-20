"""前端 UI 自动化验证（系统 Edge，无需下载浏览器）。

覆盖重构后的聊天式工作区主路径：
  资料填写 -> 新建对话 -> 贴 JD -> 生成 -> 消息流 / 雇主问题 ->
  打开简历 -> 切换各编辑 tab -> AI 修改 -> 设置弹窗 -> 移动端。
同时收集控制台错误（必须为 0）。

需要服务已在运行（默认 http://127.0.0.1:8000，可用 BASE_URL 覆盖）：
    ./.venv/Scripts/python.exe backend/tests/ui_check.py

注意：本脚本会向当前数据库写入资料与对话。请对隔离库运行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = (os.environ.get("BASE_URL") or "").strip()  # 为空则自起隔离服务
OUT = Path(__file__).resolve().parent / "shots"
OUT.mkdir(exist_ok=True)

PROFILE_NAME = "张三"

JD = """高级后端工程师
Responsibilities
- 负责高并发订单系统的设计与优化
- 与产品、数据团队协作交付

Requirements
- 3 年以上 Python 后端开发经验
- 熟悉 Kubernetes / Docker，有云原生落地经验
- 熟悉 MySQL、Redis，具备高并发系统设计能力

Working Terms
- 全职，混合办公

Employer questions
- 期望薪资是多少？
- 最快到岗时间？"""


def _run(base: str, on_exit=None) -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        page.goto(base, wait_until="load")
        # 资料为空时默认进入「01 资料页」（引导先填资料）；等待导航初始化完成即可，
        # 下方第一步本就是切到 01 资料视图填写
        page.wait_for_selector(".rail-item.is-active", timeout=15000)
        page.wait_for_timeout(800)

        # ---------- 01 资料库 ----------
        # 空库默认就在「01 资料」；若资料已存在则默认在工作区，切到「01 资料」再填写
        page.click('.rail-item[data-view="profile"]')
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "01-profile.png"))
        print("[shot] 01 资料库")

        page.fill("#pf-name", PROFILE_NAME)
        page.fill("#pf-headline", "5 年后端工程师")
        page.fill("#pf-email", "zhangsan@example.com")
        page.fill("#pf-location", "上海")
        page.fill("#pf-summary", "专注高并发后端与云原生。")
        page.click('[data-add="skills"]')
        page.wait_for_timeout(200)
        page.fill('.rep-item[data-name="skills"] input[data-field="name"]', "Python")
        page.fill('.rep-item[data-name="skills"] input[data-field="level"]', "精通")
        page.click('[data-add="experiences"]')
        page.wait_for_timeout(200)
        page.fill('.rep-item[data-name="experiences"] input[data-field="company"]', "ACME 科技")
        page.fill('.rep-item[data-name="experiences"] input[data-field="title"]', "高级后端工程师")
        page.fill('.rep-item[data-name="experiences"] textarea[data-field="achievements"]',
                  "把核心接口 P99 从 800ms 降到 120ms")
        page.click("#btn-save-profile")
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "02-profile-filled.png"))
        print("[shot] 02 资料已填写")

        # ---------- 03 新建对话 ----------
        page.click('.rail-item[data-view="workspace"]')
        page.wait_for_timeout(300)
        page.click("#btn-new-chat")
        page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "03-chat-new.png"))
        print("[shot] 03 新建对话")

        # ---------- 04 填写职位名称 + 岗位要求 ----------
        # 现为两个独立输入：职位名称(#ws-title-input) + 岗位要求(#ws-input)；
        # 生成时先保存有变化的字段，再触发。故此处仅填表，JD 气泡在生成后出现。
        page.fill("#ws-title-input", "高级后端工程师")
        page.fill("#ws-input", JD)
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "04-chat-jd.png"))
        print("[shot] 04 填写职位名称与岗位要求")

        # ---------- 05 生成完成 ----------
        page.click("#btn-chat-generate")
        # 流程变更：生成前若有岗位缺口会先弹 AI 问答卡；出现即点「跳过，直接生成」。
        try:
            page.wait_for_selector("#interview-dialog[open]", timeout=6000)
            page.click("#iv-skip-all")
        except Exception:
            pass
        page.wait_for_selector('#ws-status[data-status="succeeded"]', timeout=45000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "05-chat-done.png"))
        print("[shot] 05 生成完成")

        # ---------- 06 简历所见即所得 ----------
        page.click("#btn-chat-open-resume")
        page.wait_for_selector('.view[data-view="resume"].is-active', timeout=15000)
        page.wait_for_selector("#studio-work:not([hidden])", timeout=15000)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "06-resume-wysiwyg.png"))
        print("[shot] 06 简历所见即所得")

        for tab, name in [("source", "07-source"), ("ai", "08-ai"),
                          ("layout", "09-layout"), ("analysis", "10-analysis")]:
            page.click(f'.tab[data-tab="{tab}"]')
            page.wait_for_timeout(600)
            page.screenshot(path=str(OUT / f"{name}.png"))
            print(f"[shot] {name}")

        # ---------- 11 AI 修改 ----------
        page.click('.tab[data-tab="ai"]')
        page.fill("#ai-instruction", "切换深色")
        page.click("#btn-ai-run")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "11-ai-edited.png"))
        print("[shot] 11 AI 修改后")

        # ---------- 12 设置弹窗 ----------
        page.click("#btn-settings")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "12-settings.png"))
        print("[shot] 12 设置弹窗")

        # ---------- 13 移动端 ----------
        page.set_viewport_size({"width": 420, "height": 860})
        page.keyboard.press("Escape")
        page.click('.rail-item[data-view="profile"]')
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "13-mobile.png"))
        print("[shot] 13 移动端")

        # 收尾：直接以最终结论退出。headless Edge 在本机偶发在关闭期挂起 driver 连接，
        # 该现象不影响本次页面校验结果，故不等待浏览器优雅关闭。
        code = 1 if errors else 0
        if errors:
            print("\n⚠️  控制台错误：")
            for e in errors[:20]:
                print("   ", e)
        else:
            print("\nUI 验证通过，无控制台错误 ✅")
        sys.stdout.flush()
        # os._exit 会跳过 with/finally 清理，因此先显式收尾（终止隔离服务）
        if on_exit:
            on_exit()
        os._exit(code)


def main() -> int:
    """默认自起隔离服务；显式设置 BASE_URL 时才用外部服务。"""
    explicit = (os.environ.get("BASE_URL") or "").strip()
    if explicit:
        print(f"[外部服务] {explicit} —— 若指向 8000，测试数据会写入用户真实库")
        return _run(explicit)
    from _ui_server import spawn_isolated_server, stop_isolated_server
    proc, base, db_file = spawn_isolated_server("ui_check", 8121)
    try:
        return _run(base, on_exit=lambda: stop_isolated_server(proc, db_file))
    except BaseException:
        stop_isolated_server(proc, db_file)
        raise


if __name__ == "__main__":
    sys.exit(main())
