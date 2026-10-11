"""聊天式工作区端到端验证（真实浏览器：headless Edge）。

覆盖 T04 前端重构的关键路径，并强制断言「控制台 0 error」：
  资料页 / 工作区 / 简历 三视图导航 →
  新建对话 → 贴入 JD（含 Responsibilities / Requirements / Working Terms /
  Employer questions 分节）→ 生成 → 消息流 + 雇主问题折叠卡 →
  打开简历 → 匹配分析里的覆盖清单渲染 → 设置弹窗含「最大输出 token」。

需要服务已在运行（默认 http://127.0.0.1:8010，可用 BASE_URL 覆盖）：
    NO_PROXY=localhost,127.0.0.1 DATABASE_URL=sqlite:///<隔离库> \\
      ./.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --port 8010
    NO_PROXY=localhost,127.0.0.1 ./.venv/Scripts/python.exe backend/tests/chat_e2e.py

注意：本脚本会向当前数据库写入资料与对话。请对隔离库运行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8010")
SHOTS = Path(__file__).resolve().parent / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)

OK: list[str] = []
BAD: list[str] = []

# 覆盖设计文档要求的四个分节：Responsibilities / Requirements / Working Terms / Employer questions
JD = (
    "Senior Backend Engineer\n"
    "About the role\n"
    "We are looking for a senior backend engineer to join our platform team.\n"
    "\n"
    "Responsibilities\n"
    "- Design and build scalable backend services in Python\n"
    "- Own the reliability of our order processing pipeline\n"
    "- Collaborate with product and data teams\n"
    "\n"
    "Requirements\n"
    "- 5+ years of backend development experience\n"
    "- Strong proficiency in Python and SQL\n"
    "- Experience with Kubernetes and Docker\n"
    "- Familiarity with Kafka or other message queues\n"
    "\n"
    "Working Terms\n"
    "- Full-time, hybrid (3 days on-site)\n"
    "- Competitive salary and annual bonus\n"
    "\n"
    "Employer questions\n"
    "- What is your expected salary?\n"
    "- When can you start?\n"
    "- Do you require visa sponsorship?\n"
)

PROFILE = {
    "name": "张三", "headline": "高级后端工程师", "email": "zhangsan@example.com",
    "phone": "13800000000", "location": "上海",
    "summary": "5 年后端开发，专注高并发与云原生。",
    "skills": [{"name": "Python", "level": "精通", "category": "语言"},
               {"name": "Kubernetes", "level": "熟练", "category": "工具"},
               {"name": "SQL", "level": "熟练", "category": "语言"}],
    "experiences": [{"company": "ACME 科技", "title": "高级后端工程师", "start": "2021.03",
                     "end": "至今", "summary": "负责订单核心链路重构。",
                     "achievements": ["把核心接口 P99 从 800ms 降到 120ms"],
                     "tech": ["Python", "Kubernetes", "Kafka"]}],
    "education": [{"school": "某大学", "degree": "本科", "major": "计算机",
                   "start": "2014", "end": "2018", "highlights": ["GPA 3.7"]}],
}


def check(name: str, cond: bool, detail: object = "") -> None:
    (OK if cond else BAD).append(f"{name}  {detail}")
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=60)
    try:
        c.get("/api/health")
    except Exception as exc:  # pragma: no cover
        print(f"无法连接 {BASE}，请先启动服务。({exc})")
        return 2

    # 先落全局资料，确保新建对话的快照有素材
    c.put("/api/profile", json=PROFILE)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        print("\n=== 1. 加载应用与三视图导航 ===")
        page.goto(BASE, wait_until="load")
        page.wait_for_selector("#chat-list", timeout=15000)
        page.wait_for_timeout(400)
        rail = page.eval_on_selector_all(".rail-item", "els => els.map(e => e.dataset.view)")
        check("导航为 资料/工作区/简历 三项", rail == ["profile", "workspace", "resume"], rail)
        check("默认视图为工作区",
              page.eval_on_selector('.view[data-view="workspace"]', "e => e.classList.contains('is-active')") is True)

        print("\n=== 2. 新建对话 ===")
        page.click("#btn-new-chat")
        page.wait_for_selector("#ws-work:not([hidden])", timeout=15000)
        page.wait_for_timeout(300)
        check("会话面板已显示", page.is_visible("#ws-work") is True)
        check("资料快照栏显示姓名",
              "张三" in (page.text_content("#ws-profile-name") or ""), page.text_content("#ws-profile-name"))

        print("\n=== 3. 填写职位名称 + 岗位要求（两个独立输入）===")
        page.fill("#ws-title-input", "Senior Backend Engineer")
        page.fill("#ws-input", JD)
        page.wait_for_timeout(200)
        check("职位名称已填入",
              page.input_value("#ws-title-input") == "Senior Backend Engineer",
              page.input_value("#ws-title-input"))
        check("岗位要求已填入（含四个分节）", "Responsibilities" in page.input_value("#ws-input"))
        check("填好后生成按钮可用",
              page.get_attribute("#btn-chat-generate", "disabled") is None)

        print("\n=== 4. 生成简历（先保存有变化的字段，再生成）===")
        page.click("#btn-chat-generate")
        page.wait_for_selector('#ws-status[data-status="succeeded"]', timeout=45000)
        page.wait_for_timeout(400)
        check("状态为已完成",
              page.get_attribute("#ws-status", "data-status") == "succeeded")
        check("消息流出现 JD 气泡（生成时同步保存岗位要求）", page.is_visible(".ws-bubble-jd") is True)
        check("会话标题同步为职位名称",
              "Senior Backend Engineer" in (page.text_content("#ws-title") or ""),
              page.text_content("#ws-title"))
        check("出现「打开简历」按钮", page.is_visible("#btn-chat-open-resume") is True)

        print("\n=== 5. 雇主问题折叠卡（不进简历正文）===")
        eq_visible = page.is_visible("#ws-eq")
        eq_count = (page.text_content("#ws-eq-count") or "0").strip()
        check("雇主问题卡可见", eq_visible is True)
        check("识别到雇主问题（>=1）", eq_count.isdigit() and int(eq_count) >= 1, eq_count)

        page.screenshot(path=str(SHOTS / "chat_workspace.png"), full_page=False)

        print("\n=== 6. 打开简历 + 覆盖清单渲染 ===")
        page.click("#btn-chat-open-resume")
        page.wait_for_selector('.view[data-view="resume"].is-active', timeout=15000)
        page.wait_for_selector("#studio-work:not([hidden])", timeout=15000)
        page.click('.tab[data-tab="analysis"]')
        page.wait_for_selector(".cov-item", timeout=10000)
        page.wait_for_timeout(300)
        n_cov = page.eval_on_selector_all(".cov-item", "els => els.length")
        n_grounded = page.eval_on_selector_all(".cov-badge.is-grounded", "els => els.length")
        n_stats = page.eval_on_selector_all(".cov-stats .stat", "els => els.length")
        check("覆盖清单条目渲染（>=1）", n_cov >= 1, n_cov)
        check("覆盖清单统计块为 4 格", n_stats == 4, n_stats)
        check("存在 grounded（有据可依）条目", n_grounded >= 1, n_grounded)
        # 雇主问题同样在分析页展示
        check("分析页含雇主问题区块",
              "EMPLOYER QUESTIONS" in (page.text_content("#analysis-body") or ""))

        page.screenshot(path=str(SHOTS / "resume_analysis.png"), full_page=False)

        print("\n=== 7. 设置弹窗含「最大输出 token」===")
        page.click("#btn-settings")
        page.wait_for_selector("#settings-dialog[open]", timeout=8000)
        page.wait_for_timeout(300)
        mt = page.input_value("#cfg-max-tokens")
        check("最大输出 token 默认 0（不限制）", mt == "0", mt)
        page.screenshot(path=str(SHOTS / "settings_max_tokens.png"), full_page=False)
        page.click("#btn-close-settings")
        page.wait_for_timeout(200)

        print("\n=== 8. 控制台无 error / warning ===")
        check("控制台 0 条 error/warning", not console, console[:4])

        # 注：本机 headless Edge 偶发在 browser.close() 处挂起 driver 连接（与页面逻辑无关），
        # 故交由 with 上下文在退出时统一回收浏览器。

    print("\n" + "=" * 64)
    print(f"前端 E2E： PASS {len(OK)} / FAIL {len(BAD)}")
    if BAD:
        print("\n未通过：")
        for x in BAD:
            print("  -", x)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
