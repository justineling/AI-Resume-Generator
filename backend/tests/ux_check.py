"""UX 优化项（命令面板 / 快捷键 / 职位名识别 / 生成后引导 / 粘贴简历导入）的浏览器验证。

参考 iv_check.py：默认自起隔离服务（不污染用户库），headless Edge 真机页面，
强制断言「控制台 0 error/warning」。逐项覆盖：
  1. Ctrl+K 命令面板：打开 / 过滤 / ↓+Enter 执行「打开 简历」并切视图 / Esc 关闭
  2. 粘贴含「Senior Frontend Engineer」的 JD -> 职位名自动回填
  3. Ctrl+Enter 在岗位要求框触发生成
  4. 生成成功后主按钮变「查看简历 →」且点击打开简历；编辑字段后恢复「重新生成」
  5. 资料页「粘贴简历文本导入」-> 弹窗 -> 解析 -> 表单被填充（GET /api/profile 核对）
  6. 深色 / 浅色下命令面板可读、无横向溢出；375px 下不溢出
  7. 全程 0 控制台 error/warning

    NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/ux_check.py
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


def check(name: str, cond: bool, detail: object = "") -> None:
    (OK if cond else BAD).append(f"{name}  {detail}")
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")


PROFILE = {
    "name": "李四", "headline": "后端工程师", "email": "lisi@example.com",
    "phone": "13900000000", "location": "杭州",
    "summary": "有订单服务经验。",
    "skills": [{"name": "Python", "level": "熟练", "category": "语言"}],
    "experiences": [{"company": "ACME 科技", "title": "后端工程师", "start": "2021.03",
                     "end": "至今", "summary": "负责订单服务。",
                     "achievements": ["主导订单链路重构，接口 P99 从 800ms 降到 120ms"],
                     "tech": ["Python"]}],
}

# 第一行是职位名，用来验证「粘贴后自动回填职位名」
JD = (
    "Senior Frontend Engineer\n"
    "Responsibilities\n"
    "- Build and maintain web UI\n"
    "\n"
    "Requirements\n"
    "- 3+ years JavaScript / TypeScript\n"
    "- React and modern build tooling\n"
    "\n"
    "Working Terms\n"
    "- Full-time, hybrid\n"
)

RESUME_TEXT = (
    "王小明\n"
    "高级前端工程师 | 深圳\n"
    "邮箱: wangxm@example.com  电话: 13712345678\n"
    "GitHub: https://github.com/wangxm\n"
    "\n"
    "个人简介\n"
    "6 年前端开发经验，专注性能优化与工程化。\n"
    "\n"
    "工作经历\n"
    "2020.06 - 至今  某某科技 | 高级前端工程师\n"
    "- 主导管理后台性能优化，首屏时间从 3.2s 降到 1.1s\n"
    "- 搭建组件库与 CI 流程\n"
    "\n"
    "专业技能\n"
    "TypeScript, React, Vite, Webpack\n"
    "\n"
    "教育背景\n"
    "2014 - 2018  某某大学  软件工程\n"
)


def _run(base: str) -> int:
    c = httpx.Client(base_url=base, timeout=60)
    try:
        c.get("/api/health")
    except Exception as exc:  # pragma: no cover
        print(f"无法连接 {base}，请先启动服务。({exc})")
        return 2

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

        # ================= 1. 命令面板基础 =================
        print("\n=== 1. 命令面板（Ctrl+K / 过滤 / ↓+Enter / Esc）===")
        page.click('.rail-item[data-view="profile"]')
        page.wait_for_timeout(200)
        page.keyboard.press("Control+k")
        page.wait_for_selector("#command-palette[open]", timeout=8000)
        page.wait_for_timeout(250)
        check("Ctrl+K 打开命令面板", page.eval_on_selector("#command-palette", "e=>e.open") is True)
        check("打开后焦点进入输入框",
              page.evaluate("() => document.activeElement && document.activeElement.id") == "cmd-input")
        check("面板为 dialog 且有 aria-label",
              page.get_attribute("#command-palette", "role") in (None, "dialog")
              and bool(page.get_attribute("#command-palette", "aria-label")))
        check("列表使用 role=listbox / option",
              page.get_attribute("#cmd-list", "role") == "listbox"
              and page.eval_on_selector_all('#cmd-list [role="option"]', "els=>els.length") > 0,
              page.eval_on_selector_all('#cmd-list [role="option"]', "els=>els.length"))
        page.screenshot(path=str(SHOTS / "ux-01-command-palette-dark.png"))

        # 过滤
        page.fill("#cmd-input", "打开")
        page.wait_for_timeout(200)
        n_open = page.eval_on_selector_all('#cmd-list [role="option"]', "els=>els.length")
        check("输入即时过滤（“打开”只保留导航相关）", n_open >= 3, n_open)
        page.fill("#cmd-input", "zzzznomatch")
        page.wait_for_timeout(200)
        check("无匹配时展示空态", page.is_visible("#cmd-empty") is True)
        page.fill("#cmd-input", "打开")
        page.wait_for_timeout(200)

        # ↓ 两次高亮到「打开 简历」（前 3 项依次为 资料 / 工作区 / 简历）
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(150)
        sel_text = page.eval_on_selector(
            '#cmd-list .cmd-item[aria-selected="true"]', "e=>e.textContent")
        check("↓ 移动高亮（aria-selected 生效）", "简历" in (sel_text or ""), sel_text)
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        check("Enter 执行「打开 简历」并真的切视图",
              page.eval_on_selector('.view[data-view="resume"]', "e=>e.classList.contains('is-active')") is True)
        check("面板执行后自动关闭", page.eval_on_selector("#command-palette", "e=>e.open") is False)

        # Esc 关闭
        page.keyboard.press("Control+k")
        page.wait_for_selector("#command-palette[open]", timeout=8000)
        page.wait_for_timeout(200)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        check("Esc 关闭面板", page.eval_on_selector("#command-palette", "e=>e.open") is False)

        # ================= 2. 职位名自动识别（粘贴 JD） =================
        print("\n=== 2. 粘贴 JD -> 职位名自动回填 ===")
        page.click('.rail-item[data-view="workspace"]')
        page.wait_for_timeout(200)
        page.click("#btn-new-chat")
        page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
        page.wait_for_function(
            "() => { const t=document.querySelector('#ws-title-input'); const r=document.querySelector('#ws-input');"
            " return t && r && t.value === '' && r.value === ''; }", timeout=15000)
        check("职位名初始为空", page.input_value("#ws-title-input") == "")
        check("职位名旁有「可留空」提示",
              "可留空" in (page.text_content("#ws-title-hint") or ""), page.text_content("#ws-title-hint"))
        # 模拟粘贴：先填充内容再派发 paste 事件（与真实粘贴同样触发 paste 分支）
        page.fill("#ws-input", JD)
        page.dispatch_event("#ws-input", "paste")
        page.wait_for_timeout(300)
        check("粘贴后职位名自动回填", page.input_value("#ws-title-input") == "Senior Frontend Engineer",
              page.input_value("#ws-title-input"))
        page.screenshot(path=str(SHOTS / "ux-02-paste-title.png"))

        # ================= 3. Ctrl+Enter 触发生成 =================
        print("\n=== 3. Ctrl+Enter 在岗位要求框触发生成 ===")
        page.focus("#ws-input")
        page.keyboard.press("Control+Enter")
        triggered = False
        try:
            page.wait_for_selector("#interview-dialog[open]", timeout=8000)
            triggered = True
            page.wait_for_timeout(200)
            page.click("#iv-skip-all")
        except Exception:
            # 资料已覆盖 -> 直接生成，此时状态应已离开 draft
            try:
                page.wait_for_function(
                    "() => ['queued','running','succeeded'].includes("
                    "document.querySelector('#ws-status').dataset.status)", timeout=8000)
                triggered = True
            except Exception:
                triggered = False
        check("Ctrl+Enter 触发生成（弹问答卡或直接进入生成态）", triggered is True)

        # ================= 4. 生成成功后 -> 查看简历 =================
        print("\n=== 4. 生成成功后的下一步引导 ===")
        page.wait_for_selector('#ws-status[data-status="succeeded"]', timeout=90000)
        page.wait_for_timeout(600)
        check("主按钮文案变为「查看简历 →」",
              (page.text_content("#btn-chat-generate") or "").strip() == "查看简历 →",
              page.text_content("#btn-chat-generate"))
        check("次级「重新生成」按钮出现", page.is_visible("#btn-chat-regenerate") is True)
        page.screenshot(path=str(SHOTS / "ux-03-succeeded-view-resume.png"))
        page.click("#btn-chat-generate")
        page.wait_for_selector('.view[data-view="resume"].is-active', timeout=15000)
        page.wait_for_selector("#studio-work:not([hidden])", timeout=15000)
        check("点击「查看简历 →」真的打开简历", page.is_visible("#studio-work") is True)

        # 编辑字段 -> 恢复「重新生成」；改回原值 -> 再次变为「查看简历 →」
        page.click('.rail-item[data-view="workspace"]')
        page.wait_for_timeout(300)
        page.fill("#ws-title-input", "高级前端工程师（改）")
        page.wait_for_timeout(200)
        check("编辑职位名后恢复为「重新生成」",
              "重新生成" in (page.text_content("#btn-chat-generate") or ""),
              page.text_content("#btn-chat-generate"))
        check("编辑后次级按钮隐藏", page.is_visible("#btn-chat-regenerate") is False)
        page.fill("#ws-title-input", "Senior Frontend Engineer")
        page.wait_for_timeout(200)
        check("改回原值后再次变为「查看简历 →」",
              (page.text_content("#btn-chat-generate") or "").strip() == "查看简历 →",
              page.text_content("#btn-chat-generate"))

        # ================= 5. 粘贴简历文本导入资料 =================
        print("\n=== 5. 资料页「粘贴简历文本导入」 ===")
        page.click('.rail-item[data-view="profile"]')
        page.wait_for_timeout(300)
        check("资料页有「粘贴简历文本导入」按钮", page.is_visible("#btn-profile-import-text") is True)
        page.click("#btn-profile-import-text")
        page.wait_for_selector("#resume-import-dialog[open]", timeout=8000)
        page.wait_for_timeout(200)
        check("弹窗含覆盖提醒",
              "覆盖" in (page.text_content("#resume-import-dialog") or ""))
        page.fill("#ri-text", RESUME_TEXT)
        page.click("#btn-import-text-run")
        page.wait_for_function("() => !document.querySelector('#resume-import-dialog').open", timeout=30000)
        page.wait_for_timeout(500)
        prof = c.get("/api/profile").json()
        check("导入后资料已写库（姓名）", prof.get("name") == "王小明", prof.get("name"))
        check("导入后解析出工作经历（>=1）", len(prof.get("experiences") or []) >= 1,
              len(prof.get("experiences") or []))
        check("导入后解析出技能（>=1）", len(prof.get("skills") or []) >= 1,
              len(prof.get("skills") or []))
        check("资料表单被回填（#pf-name）", page.input_value("#pf-name") == "王小明",
              page.input_value("#pf-name"))
        page.screenshot(path=str(SHOTS / "ux-04-profile-import.png"))

        # ================= 6. 深色 / 浅色 / 移动端 可读性 + 无溢出 =================
        print("\n=== 6. 命令面板可读性 + 无横向溢出（深色/浅色/375px）===")
        page.keyboard.press("Control+k")
        page.wait_for_selector("#command-palette[open]", timeout=8000)
        page.wait_for_timeout(250)
        dark_bg = page.eval_on_selector(
            '#cmd-list .cmd-item[aria-selected="true"]', "e=>getComputedStyle(e).backgroundColor")
        dark_no_overflow = page.evaluate(
            "() => { const d=document.querySelector('#command-palette');"
            " return d.scrollWidth <= d.clientWidth + 1"
            " && document.documentElement.scrollWidth <= window.innerWidth + 1; }")
        check("深色：选中项有可见底色（--signal-tint-strong 生效）",
              dark_bg not in (None, "rgba(0, 0, 0, 0)", "transparent"), dark_bg)
        check("深色：面板与页面无横向溢出", dark_no_overflow is True)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        # 切浅色
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]", timeout=8000)
        page.select_option("#cfg-theme", "light")
        page.dispatch_event("#cfg-theme", "change")
        page.wait_for_timeout(300)
        page.click("#btn-close-settings")
        page.wait_for_timeout(200)
        check("已切到浅色主题",
              page.eval_on_selector("html", "e=>e.getAttribute('data-theme')") == "light")
        page.keyboard.press("Control+k")
        page.wait_for_selector("#command-palette[open]", timeout=8000)
        page.wait_for_timeout(250)
        light_bg = page.eval_on_selector(
            '#cmd-list .cmd-item[aria-selected="true"]', "e=>getComputedStyle(e).backgroundColor")
        light_fg = page.eval_on_selector("#cmd-input", "e=>getComputedStyle(e).color")
        light_no_overflow = page.evaluate(
            "() => { const d=document.querySelector('#command-palette');"
            " return d.scrollWidth <= d.clientWidth + 1"
            " && document.documentElement.scrollWidth <= window.innerWidth + 1; }")
        check("浅色：选中项有可见底色", light_bg not in (None, "rgba(0, 0, 0, 0)", "transparent"), light_bg)
        check("浅色：输入框文字色非空", bool(light_fg) and light_fg != "rgba(0, 0, 0, 0)", light_fg)
        check("浅色：面板与页面无横向溢出", light_no_overflow is True)
        page.screenshot(path=str(SHOTS / "ux-05-command-palette-light.png"))

        # 375px
        page.set_viewport_size({"width": 375, "height": 780})
        page.wait_for_timeout(400)
        mobile_no_overflow = page.evaluate(
            "() => { const d=document.querySelector('#command-palette');"
            " return d.scrollWidth <= d.clientWidth + 1"
            " && d.getBoundingClientRect().width <= window.innerWidth + 1"
            " && document.documentElement.scrollWidth <= window.innerWidth + 1; }")
        check("375px：面板与页面无横向溢出", mobile_no_overflow is True)
        page.screenshot(path=str(SHOTS / "ux-06-command-palette-mobile.png"))
        # 移动端也能执行命令（点第一项）
        page.click('#cmd-list .cmd-item')
        page.wait_for_timeout(400)

        print("\n=== 7. 控制台无 error / warning ===")
        check("控制台 0 条 error/warning", not console, console[:4])

    print("\n" + "=" * 64)
    print(f"UX 验证： PASS {len(OK)} / FAIL {len(BAD)}")
    if BAD:
        print("\n未通过：")
        for x in BAD:
            print("  -", x)
    sys.stdout.flush()
    return 1 if BAD else 0


def main() -> int:
    explicit = (os.environ.get("BASE_URL") or "").strip()
    if explicit:
        print(f"[外部服务] {explicit} —— 若指向 8000，测试数据会写入用户真实库")
        return _run(explicit)
    from _ui_server import isolated_server
    with isolated_server("ux_check", 8127) as base:
        return _run(base)


if __name__ == "__main__":
    raise SystemExit(main())
