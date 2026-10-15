"""UX 优化项（命令面板 / 快捷键 / 职位名识别 / 生成后引导 / 粘贴简历导入）的浏览器验证。

参考 iv_check.py：默认自起隔离服务（不污染用户库），headless Edge 真机页面，
强制断言「控制台 0 error/warning」。逐项覆盖：
  1. Ctrl+K 命令面板：打开 / 过滤 / ↓+Enter 执行「打开 简历」并切视图 / Esc 关闭
  2. 粘贴含「Senior Frontend Engineer」的 JD -> 职位名自动回填
  3. Ctrl+Enter 在岗位要求框触发生成
  4. 生成成功后主按钮变「查看简历 →」且点击打开简历；编辑字段后恢复「重新生成」
  5. 资料页「粘贴简历文本导入」-> 弹窗 -> 解析 -> 表单被填充（GET /api/profile 核对）
  6. 深色 / 浅色下命令面板可读、无横向溢出；375px 下不溢出
  7. 工作区粘贴简历原文 -> 确认条（只增补不覆盖）-> 确认 -> 快照写入经历/技能；
     含「不覆盖」与「粘贴 JD 不误触发」两条反面断言；以及慢网络下新建对话期间
     的输入不被覆盖（openChat 回填竞态）
  8. 命令面板粘贴 URL -> 「回车即抓取」提示 -> 回车复用导入流程；Esc 两段式退出
  9. 全程 0 控制台 error/warning

    NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/ux_check.py
"""
from __future__ import annotations

import json
import os
import sys
import time
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

# 工作区粘贴用的简历文本：刻意拉长（> 300 字符）+ 含多个章节词，
# 用于验证「像一份简历」的启发式判定。
RESUME_PASTE = (
    "赵晓峰\n"
    "资深全栈工程师 | 上海 | 13800001111 | zhaoxf@example.com\n"
    "个人主页: https://github.com/zhaoxf\n"
    "\n"
    "个人简介\n"
    "8 年全栈开发经验，主导过多个高并发系统的架构设计与性能优化，"
    "熟悉云原生与 DevOps 实践，注重工程质量与可维护性。\n"
    "\n"
    "工作经历\n"
    "2021.03 - 至今  星辰科技 | 资深全栈工程师\n"
    "- 主导订单中台重构，接口 P99 从 800ms 降到 120ms，支撑日均千万级请求\n"
    "- 搭建前端组件库与自动化测试体系，回归缺陷率下降 60%\n"
    "- 推动容器化与灰度发布，故障回滚时间从 30 分钟降到 2 分钟\n"
    "\n"
    "2018.07 - 2021.02  云图信息 | 全栈工程师\n"
    "- 负责数据可视化平台前端架构，首屏时间优化 45%\n"
    "\n"
    "项目经历\n"
    "统一网关\n"
    "- 为 30+ 内部服务提供统一鉴权与限流\n"
    "\n"
    "专业技能\n"
    "TypeScript, React, Node.js, Python, PostgreSQL, Redis, Docker, Kubernetes\n"
    "\n"
    "教育背景\n"
    "2014 - 2018  某某大学  软件工程\n"
)

# 命令面板 URL 模式用的「快速失败」链接（本机未监听端口，抓取会立即被拒）。
CMD_URL = "http://127.0.0.1:9/nope"


def _dispatch_paste(page, selector: str, text: str) -> None:
    """派发一个带 clipboardData 的合成 paste 事件（Playwright 无直接设置剪贴板 API）。"""
    page.evaluate(
        """([sel, text]) => {
            const el = document.querySelector(sel);
            const dt = new DataTransfer();
            dt.setData('text/plain', text);
            el.dispatchEvent(new ClipboardEvent('paste', {
                clipboardData: dt, bubbles: true, cancelable: true,
            }));
        }""",
        [selector, text],
    )


def _newest_chat_id(client: httpx.Client) -> int:
    """取当前最新的对话 id（刚新建的那条）。"""
    chats = client.get("/api/chats").json()
    return max(int(ch["id"]) for ch in chats)



def _new_chat(page) -> None:
    """点「新建」并**确认新对话已真正打开**，再继续操作。

    两个坑都要绕开：
    1. 若当前 composer 有未保存内容，新建会弹 confirm（会被 Playwright 默认“取消”而中止）
       —— 这里注册一次性 dialog 处理器接受它；
    2. 新建是异步的（POST -> 刷新列表 -> openChat）。若上一个对话的 composer 恰好为空，
       「等输入框为空」会立刻通过、抢在 openChat 之前，随后 fillComposer 覆盖刚填入的内容。
       故改为等 #ws-code 变成新的 CHAT-xxxx（renderHead 在 fillComposer 之后执行）。
    """
    prev = page.text_content("#ws-code") or ""

    def _accept(d):
        d.accept()

    page.on("dialog", _accept)
    try:
        page.click("#btn-new-chat")
        page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
        page.wait_for_function(
            "(before) => { const c = document.querySelector('#ws-code');"
            " return !!c && !!c.textContent && c.textContent !== before; }",
            arg=prev, timeout=15000)
        page.wait_for_function(
            "() => { const t = document.querySelector('#ws-title-input');"
            " const r = document.querySelector('#ws-input');"
            " return t && r && t.value === '' && r.value === ''; }", timeout=15000)
    finally:
        page.remove_listener("dialog", _accept)


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

        # ================= 7. 工作区粘贴简历原文 -> 解析并写入本对话资料 =================
        print("\n=== 7. 工作区粘贴简历原文 -> 解析并写入本对话资料（只增补，不覆盖）===")
        page.set_viewport_size({"width": 1440, "height": 960})
        page.click('.rail-item[data-view="workspace"]')
        page.wait_for_timeout(300)
        _new_chat(page)
        cid_a = _newest_chat_id(c)

        # 7.1 粘贴简历 -> 确认条 -> 确认 -> 写入快照
        _dispatch_paste(page, "#ws-stream", RESUME_PASTE)
        page.wait_for_selector("#resume-paste-dialog[open]", timeout=8000)
        page.wait_for_timeout(200)
        dlg_text = page.text_content("#resume-paste-dialog") or ""
        check("粘贴简历文本 -> 弹出确认（含「只增补，不覆盖」）", "只增补" in dlg_text, dlg_text[:60])
        page.screenshot(path=str(SHOTS / "ux-07-paste-resume-confirm.png"))
        page.click("#btn-resume-paste-confirm")
        page.wait_for_function("() => !document.querySelector('#resume-paste-dialog').open", timeout=30000)
        page.wait_for_timeout(700)
        check("确认后 toast 提示「已解析并写入本对话资料」",
              "已解析并写入本对话资料" in (page.text_content("#toasts") or ""),
              (page.text_content("#toasts") or "")[:60])
        snap_a = (c.get(f"/api/chats/{cid_a}").json().get("profile_snapshot") or {})
        check("确认后快照写入工作经历（>=1）", len(snap_a.get("experiences") or []) >= 1,
              len(snap_a.get("experiences") or []))
        check("确认后快照写入技能（>=1）", len(snap_a.get("skills") or []) >= 1,
              len(snap_a.get("skills") or []))

        # 7.2 不覆盖：先给对话设一个已有字段，再导入，验证该字段未被改写
        _new_chat(page)
        cid_b = _newest_chat_id(c)
        c.put(f"/api/chats/{cid_b}/profile", json={
            "name": "不覆盖标记",
            "experiences": [{"company": "ACME 科技", "title": "后端工程师", "start": "2019", "end": "2021",
                             "achievements": ["已有经历，不应被覆盖"], "tech": ["Python"]}],
        })
        _dispatch_paste(page, "#ws-stream", RESUME_PASTE)
        page.wait_for_selector("#resume-paste-dialog[open]", timeout=8000)
        page.wait_for_timeout(150)
        page.click("#btn-resume-paste-confirm")
        page.wait_for_function("() => !document.querySelector('#resume-paste-dialog').open", timeout=30000)
        page.wait_for_timeout(600)
        snap_b = (c.get(f"/api/chats/{cid_b}").json().get("profile_snapshot") or {})
        check("不覆盖：已设定的姓名未被改写", snap_b.get("name") == "不覆盖标记", snap_b.get("name"))
        check("不覆盖：解析出的经历被追加且原有经历仍在",
              len(snap_b.get("experiences") or []) >= 2
              and any(e.get("company") == "ACME 科技" for e in (snap_b.get("experiences") or [])),
              len(snap_b.get("experiences") or []))

        # 7.3 岗位要求（非简历）粘贴：不误触发简历解析，保持原路径
        _new_chat(page)
        # (a) 在岗位要求框内粘贴 -> 保持既有行为（填入 + 自动回填职位名），不弹简历确认
        page.fill("#ws-input", JD)
        page.dispatch_event("#ws-input", "paste")
        page.wait_for_timeout(300)
        check("岗位要求框内粘贴 -> 不弹简历确认", page.eval_on_selector("#resume-paste-dialog", "e=>e.open") is False)
        check("岗位要求框内粘贴 -> 仍自动回填职位名",
              page.input_value("#ws-title-input") == "Senior Frontend Engineer",
              page.input_value("#ws-title-input"))
        # (b) 在工作区空白处粘贴 JD（非简历文本）-> 不弹简历确认
        _dispatch_paste(page, "#ws-stream", JD)
        page.wait_for_timeout(300)
        check("工作区粘贴 JD（非简历）-> 不弹简历确认",
              page.eval_on_selector("#resume-paste-dialog", "e=>e.open") is False)

        # 7.4 新建对话期间的输入不被覆盖（模拟慢网络）
        # 产品缺陷：createChat 网络往返期间 UI 可交互，openChat 回来时若用新对话的
        # 空值覆盖 composer，会吞掉用户刚打的字。用 page.route 给 POST /api/chats
        # 加 1.5s 延迟，点「新建」后立刻打字，验证输入仍在。
        print("\n=== 7.4 新建对话期间输入不被覆盖（慢网络）===")
        _dlg: list[str] = []

        def _accept_dialog(d):
            _dlg.append(d.message)
            d.accept()

        def _slow_post_chats(route):
            if route.request.method == "POST":
                time.sleep(1.5)
            route.continue_()

        page.on("dialog", _accept_dialog)
        page.route("**/api/chats", _slow_post_chats)
        try:
            page.click("#btn-new-chat")          # 若 composer 有未保存内容会弹 confirm，此处接受
            page.wait_for_timeout(150)           # 处于 creatingChat 窗口（POST 仍挂起）
            page.fill("#ws-input", "SLOWTYPED 新建期间输入的内容")
            page.wait_for_timeout(2500)          # 等 createChat + openChat 完成
            check("慢网络下新建期间的输入未被覆盖",
                  "SLOWTYPED" in page.input_value("#ws-input"),
                  page.input_value("#ws-input")[:36])
        finally:
            page.unroute("**/api/chats")
            page.remove_listener("dialog", _accept_dialog)

        # ================= 8. 命令面板粘贴 URL -> 回车即抓取 =================
        print("\n=== 8. 命令面板粘贴 URL -> 回车即抓取该链接 ===")
        page.click('.rail-item[data-view="workspace"]')
        page.wait_for_timeout(200)
        page.keyboard.press("Control+k")
        page.wait_for_selector("#command-palette[open]", timeout=8000)
        page.wait_for_timeout(250)
        _dispatch_paste(page, "#cmd-input", CMD_URL)
        page.wait_for_timeout(300)
        check("粘贴 URL -> 出现「回车即抓取该链接」提示",
              page.is_visible("#cmd-url-hint") is True
              and "回车即抓取" in (page.text_content("#cmd-url-hint") or ""),
              (page.text_content("#cmd-url-hint") or ""))
        check("提示中显示该链接", CMD_URL in (page.text_content("#cmd-url-value") or ""),
              page.text_content("#cmd-url-value"))
        n_opts = page.eval_on_selector_all('#cmd-list [role="option"]', "els=>els.length")
        first_opt = page.text_content('#cmd-list [role="option"]') or ""
        check("URL 模式仅保留「导入该链接」一条命令", n_opts == 1 and "导入该链接" in first_opt,
              f"{n_opts} :: {first_opt[:40]}")
        page.screenshot(path=str(SHOTS / "ux-08-cmd-url-mode.png"))
        # 第一次 Esc -> 退出 URL 模式（面板仍开）
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        check("第一次 Esc 退出 URL 模式（面板仍开）",
              page.eval_on_selector("#command-palette", "e=>e.open") is True
              and page.is_visible("#cmd-url-hint") is False)
        # 再次粘贴 URL 并回车 -> 复用「从链接导入」流程
        _dispatch_paste(page, "#cmd-input", CMD_URL)
        page.wait_for_timeout(250)
        page.keyboard.press("Enter")
        page.wait_for_timeout(700)
        check("回车后命令面板关闭", page.eval_on_selector("#command-palette", "e=>e.open") is False)
        check("回车后打开导入框并填入该 URL",
              page.is_visible("#import-url-box") is True
              and page.input_value("#import-url-input") == CMD_URL,
              page.input_value("#import-url-input"))
        # 收尾：关闭导入框，避免影响后续控制台断言
        page.click("#btn-import-cancel")
        page.wait_for_timeout(300)

        print("\n=== 9. 控制台无 error / warning ===")
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
