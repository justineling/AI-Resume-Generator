"""端到端验证（浏览器层 + 安全边界）。

与 regression.py 的分工：
- regression.py：API 层断言，用 TestClient，不需要服务器。
- 本脚本：**需要真实浏览器**才能验证的部分——版式在浏览器里的实际布局几何、
  净化器与 CSP 双层防御是否真的阻止脚本执行、CSP 是否破坏了简历渲染。

需要服务已在运行（默认 http://127.0.0.1:8000，可用环境变量 BASE_URL 覆盖）：
    ./.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --port 8000
    ./.venv/Scripts/python.exe backend/tests/e2e_verify.py

注意：本脚本会向当前数据库写入资料与任务。请勿对生产数据运行。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sanitize import sanitize_fragment  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
OK: list[str] = []
BAD: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (OK if cond else BAD).append(f"{name}  {detail}")
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")


def wait_task(c: httpx.Client, tid: int, timeout: float = 40) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        t = c.get(f"/api/tasks/{tid}").json()
        if t["status"] in ("succeeded", "failed", "cancelled"):
            return t
        time.sleep(0.2)
    return c.get(f"/api/tasks/{tid}").json()


def save_profile(c: httpx.Client) -> None:
    c.put("/api/profile", json={
        "name": "张三", "headline": "后端工程师", "email": "a@b.com", "location": "上海",
        "skills": [{"name": "Python", "level": "精通", "category": "语言"},
                   {"name": "Kubernetes", "level": "熟练", "category": "工具"}],
        "experiences": [{"company": "ACME", "title": "工程师", "start": "2021", "end": "2024",
                         "achievements": ["P99 降低 80%"], "tech": ["Python"]}],
        "education": [{"school": "某大学", "degree": "本科", "major": "CS",
                       "start": "2014", "end": "2018", "highlights": ["GPA 3.7"]}],
    })


def new_resume(c: httpx.Client, title: str, jd: str) -> tuple[int, int]:
    t = wait_task(c, c.post("/api/tasks", json={"title": title, "raw_text": jd}).json()["id"])
    assert t["status"] == "succeeded", f"生成失败：{t['error']}"
    return t["id"], t["resume_id"]


# 已知的净化绕过手法（QA 在第二轮审计中发现并实测触发过）
BYPASSES = {
    "斜杠分隔事件属性": '<img/onerror="alert(1)" src=x>',
    "svg onload（带引号）": '<svg/onload="alert(1)"></svg>',
    "svg onload（无引号）": '<svg/onload=alert(1)>',
    "十六进制实体协议": '<a href="jav&#x61;script:alert(1)">a</a>',
    "十进制实体协议": '<a href="&#106;avascript:alert(1)">b</a>',
    "斜杠分隔 onclick": '<div/onclick="alert(1)">c</div>',
}

LEGIT_FRAGMENT = (
    '<style>.resume{color:#111}@page{size:A4;margin:12mm}'
    '@media print{.r-section{break-inside:avoid}}</style>'
    '<div class="resume" data-theme="ark" data-layout="sidebar-left">'
    '<img src="data:image/png;base64,iVBORw0KGgo=" alt="头像" />'
    '<a href="https://github.com/me">GitHub</a>'
    '<section data-section-id="skills" data-section-type="skills">'
    '<h2 class="r-section-title">技能</h2><ul class="r-tags"><li>Python</li></ul></section></div>'
)


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=60)
    try:
        c.get("/api/health")
    except Exception as exc:
        print(f"无法连接 {BASE}，请先启动服务。({exc})")
        return 2

    save_profile(c)

    print("\n=== 1. 净化器：已知绕过手法必须全部中和 ===")
    for label, payload in BYPASSES.items():
        clean, _ = sanitize_fragment(payload)
        low = clean.lower()
        safe = not re.search(r"on[a-z]+\s*=", low) and "javascript:" not in low and "<svg" not in low
        check(f"中和 {label}", safe, f"-> {clean.strip()[:52]!r}")
    clean2, _ = sanitize_fragment('<a href="&amp;#106;avascript:alert(1)">x</a>')
    check("双重实体未被还原", "javascript:" not in clean2.lower())

    print("\n=== 2. 净化器：正常简历片段必须无损 ===")
    clean3, removed3 = sanitize_fragment(LEGIT_FRAGMENT)
    for tok in ['<style>', '@page', '@media print', 'class="resume"', 'data-layout="sidebar-left"',
                'data-section-id', 'src="data:image/png;base64,', 'href="https://github.com/me"', '<li>Python</li>']:
        check(f"保留 {tok}", tok in clean3)
    check("removed 探测器不误报", removed3 is False, f"removed={removed3}")

    print("\n=== 3. preview 文档的 CSP ===")
    _, rid = new_resume(c, "E2E 岗位", "- 3年 Python 经验\n- 熟悉 Kubernetes\n- 熟悉 MySQL")
    pv = c.get(f"/api/resumes/{rid}/preview").text
    check("含 Content-Security-Policy", "Content-Security-Policy" in pv)
    check("含 default-src 'none'", "default-src 'none'" in pv)
    check("含 style-src 'unsafe-inline'", "style-src 'unsafe-inline'" in pv)
    check("未含 frame-ancestors（避免控制台警告）", "frame-ancestors" not in pv)

    print("\n=== 4. AI 编辑注入 → 入库净化 + 浏览器不执行 ===")
    evil = ('<style>.resume{color:#111}</style><div class="resume"><h1 class="r-name">张三</h1>'
            '<p>正常内容</p><img/onerror="document.title=\'PWNED\'" src=x>'
            '<svg/onload="document.title=\'PWNED\'"></svg>'
            '<a href="jav&#x61;script:document.title=\'PWNED\'">link</a></div>')
    c.post(f"/api/resumes/{rid}/ai-edit", json={"instruction": "注入测试", "current_html": evil})
    stored = c.get(f"/api/resumes/{rid}").json()["html"].lower()
    check("入库无 onerror", "onerror" not in stored)
    check("入库无 svg/script", "<svg" not in stored and "<script" not in stored)
    check("入库无 javascript:", "javascript:" not in stored)
    check("入库保留正常结构", "r-name" in stored and "正常内容" in stored)

    # 故意绕过净化器（直接 PUT 未净化内容），验证 CSP 兜底
    c.put(f"/api/resumes/{rid}", json={"html": evil})
    check("绕过净化后 payload 确实进了 preview", "onerror" in c.get(f"/api/resumes/{rid}/preview").text.lower())

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 1200})
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        # --- 恶意内容页：CSP 应拦截，控制台应出现 CSP 违规（这正是生效的证据）---
        page.goto(f"{BASE}/api/resumes/{rid}/preview", wait_until="load")
        page.wait_for_timeout(700)
        check("CSP 兜底：脚本未执行", not page.title().startswith("PWNED"), f"title={page.title()[:36]!r}")
        check("CSP 兜底：内容仍渲染", page.evaluate('document.querySelector(".resume").getBoundingClientRect().height') > 100)
        check("CSP 确实拦下了内联事件处理器", any("Content Security Policy" in m for m in console),
              f"捕获 {len(console)} 条 CSP 违规")

        # --- 干净简历页：重置计数后，不应有任何 console error/warning ---
        console.clear()
        print("\n=== 5. 真实生成简历在 CSP 下的样式完整性 ===")
        _, rid_ok = new_resume(c, "样式检查", "- Python\n- Docker")
        page.goto(f"{BASE}/api/resumes/{rid_ok}/preview", wait_until="load")
        page.wait_for_timeout(600)
        check(".r-name font-weight=800", page.evaluate('getComputedStyle(document.querySelector(".r-name")).fontWeight') == "800")
        check(".r-body 仍是 grid", page.evaluate('getComputedStyle(document.querySelector(".r-body")).display') == "grid")
        check("@page 打印规则可解析", page.evaluate(
            """() => { for (const ss of document.styleSheets) { try { for (const r of ss.cssRules) {
                 if (r.cssText.indexOf('@page') === 0) return true; } } catch(e){} } return false; }"""))
        check("正常简历页控制台无 error/warning", not console, f"{console[:2]}")

        print("\n=== 6. 5 种版式的浏览器布局几何 ===")
        sigs: dict[str, str] = {}
        for lay in ["single-column", "sidebar-left", "two-column", "timeline", "compact-grid"]:
            c.post(f"/api/resumes/{rid_ok}/layout", json={"layout": lay})
            page.goto(f"{BASE}/api/resumes/{rid_ok}/preview", wait_until="load")
            page.wait_for_timeout(420)
            sigs[lay] = page.evaluate(
                """() => {
                    const cols=[...document.querySelectorAll('.r-col')].map(e=>{const r=e.getBoundingClientRect();return Math.round(r.left)+':'+Math.round(r.width);});
                    const e=document.querySelector('.r-entry'); const cs=e?getComputedStyle(e):null;
                    const bf=e?getComputedStyle(e,'::before'):null;
                    return [getComputedStyle(document.querySelector('.r-body')).display, cols.join(','),
                            cs?cs.paddingLeft:'', bf?bf.content:''].join('|'); }""")
            print(f"      {lay:16s} {sigs[lay][:92]}")
        check("5 种版式几何两两不同", len(set(sigs.values())) == 5, f"唯一 {len(set(sigs.values()))}/5")
        browser.close()

    print("\n=== 7. 关键 API 契约回归 ===")
    _, rid3 = new_resume(c, "契约检查", "- Go\n- Kafka")
    check("非法 layout -> 400", c.post(f"/api/resumes/{rid3}/layout", json={"layout": "nope"}).status_code == 400)
    check("非法 theme -> 400", c.post(f"/api/resumes/{rid3}/layout", json={"theme": "hacker"}).status_code == 400)
    check("非法 ai-edit scope -> 422", c.post(f"/api/resumes/{rid3}/ai-edit", json={"instruction": "x", "scope": "bad"}).status_code == 422)
    check("/print 端点已移除 -> 404", c.get(f"/api/resumes/{rid3}/print").status_code == 404)

    tid_del = c.get(f"/api/tasks/{[t for t in c.get('/api/tasks').json() if t['resume_id'] == rid3][0]['id']}").json()["id"]
    check("删任务无 cascade -> 409", c.delete(f"/api/tasks/{tid_del}").status_code == 409)
    check("删任务 cascade -> 204", c.delete(f"/api/tasks/{tid_del}?cascade=true").status_code == 204)
    check("简历随之删除", c.get(f"/api/resumes/{rid3}").status_code == 404)

    print("\n" + "=" * 64)
    print(f"端到端验证： PASS {len(OK)} / FAIL {len(BAD)}")
    if BAD:
        print("\n未通过：")
        for x in BAD:
            print("  -", x)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
