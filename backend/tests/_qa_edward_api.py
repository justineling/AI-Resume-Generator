"""QA 二轮独立回归：快照生成前自动同步全局资料（f71a336 空快照 + f6fb14d 未定制跟随全局）。

独立于 regression.py 自行构造场景（资料用「李四/示例集团」，与工程师验证用
「张三/ACME」不同，避免同源盲区）。全部走 mock 降级链路（无 API Key），
隔离库 data/_qa_edward.db，绝不写用户库。

运行： ./.venv/Scripts/python.exe backend/tests/_qa_edward_api.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from _dbguard import use_isolated_db  # noqa: E402

# 必须在 import app.* 之前调用
use_isolated_db("qa_edward")

import app.fetch as fetchmod  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Profile  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  -> {extra!r}")


# 独立造的资料（与工程师验证用数据不同源）
PROFILE = {
    "name": "李四",
    "headline": "3 年测试开发工程师",
    "email": "lisi@example.com",
    "phone": "13900000000",
    "location": "杭州",
    "summary": "专注自动化测试与质量平台。",
    "skills": [{"name": "Python", "level": "熟练", "category": "语言"}],
    "experiences": [
        {
            "company": "示例集团", "title": "测试开发工程师", "start": "2022.01", "end": "至今",
            "location": "杭州", "summary": "负责回归流水线建设。",
            "achievements": ["回归时长从 2 小时降到 15 分钟"],
            "tech": ["Python", "Playwright"],
        }
    ],
    "education": [
        {"school": "某某学院", "degree": "本科", "major": "软件工程", "start": "2018", "end": "2022",
         "highlights": []}
    ],
    "projects": [],
}

JD = (
    "测试开发工程师\n"
    "Responsibilities\n- 搭建自动化回归流水线\n"
    "Requirements\n- 2 年以上 Python 经验\n- 熟悉 Playwright 或 Selenium"
)

# import-url 的抓取层 mock（与 regression.py 同款思路，但内容独立）
PAGE = {"final_url": "https://qa.example.com/job/7",
        "page_title": "QA Engineer - Example",
        "text": JD}
JD_OUT = {"job_title": "QA Engineer", "requirements": JD}


def wipe_global_profile() -> None:
    with session_scope() as session:
        for p in session.exec(select(Profile)).all():
            session.delete(p)


def sync_msgs(client: TestClient, cid: int) -> int:
    detail = client.get(f"/api/chats/{cid}").json()
    return sum(1 for m in (detail.get("messages") or [])
               if m.get("kind") == "notice" and (m.get("meta") or {}).get("type") == "snapshot_sync")


def main() -> int:
    with TestClient(app) as client:
        # ---------------- A1 原始 bug 序列：02 建对话 -> 01 填资料 -> 02 生成 ----------------
        print("\n[A1] 空全局 -> 建对话(空快照) -> 填全局 -> 加 JD -> interview")
        wipe_global_profile()
        cid1 = client.post("/api/chats", json={}).json()["id"]
        snap0 = (client.get(f"/api/chats/{cid1}").json().get("profile_snapshot") or {})
        check("建对话时全局为空 -> 快照 name 为空", not snap0.get("name"), snap0.get("name"))
        r = client.put("/api/profile", json=PROFILE)
        check("保存全局资料 -> 200", r.status_code == 200, r.status_code)
        r = client.post(f"/api/chats/{cid1}/messages", json={"kind": "jd", "content": JD})
        check("加 JD -> 201", r.status_code == 201, r.status_code)
        r = client.post(f"/api/chats/{cid1}/interview")
        check("interview -> 200（原 bug 报 400）", r.status_code == 200, r.status_code)
        d1 = client.get(f"/api/chats/{cid1}").json()
        check("快照已同步（name=李四）", (d1.get("profile_snapshot") or {}).get("name") == "李四",
              (d1.get("profile_snapshot") or {}).get("name"))
        check("快照已同步（company=示例集团）",
              (d1.get("profile_snapshot") or {}).get("experiences", [{}])[0].get("company") == "示例集团",
              (d1.get("profile_snapshot") or {}).get("experiences"))
        check("profile_dirty 仍 False", d1.get("profile_dirty") is False, d1.get("profile_dirty"))
        check("notice 恰好 1 条（snapshot_sync）", sync_msgs(client, cid1) == 1, sync_msgs(client, cid1))

        # ---------------- A4 幂等：同一对话连续两次 interview ----------------
        print("\n[A4] 幂等：同一对话再次 interview")
        r = client.post(f"/api/chats/{cid1}/interview")
        check("第二次 interview 仍 200", r.status_code == 200, r.status_code)
        check("notice 仍 1 条（不重复追加）", sync_msgs(client, cid1) == 1, sync_msgs(client, cid1))
        client.delete(f"/api/chats/{cid1}?cascade=true")

        # ---------------- A2 定制快照不被覆盖 ----------------
        print("\n[A2] 空快照+已定制(dirty=True) -> 全局填好 -> 仍 400")
        wipe_global_profile()
        cid2 = client.post("/api/chats", json={}).json()["id"]
        r = client.put(f"/api/chats/{cid2}/profile", json={})
        check("PUT chat profile json={} -> 200 且 dirty=True",
              r.status_code == 200 and r.json().get("profile_dirty") is True,
              (r.status_code, r.json().get("profile_dirty")))
        client.put("/api/profile", json=PROFILE)
        client.post(f"/api/chats/{cid2}/messages", json={"kind": "jd", "content": JD})
        r = client.post(f"/api/chats/{cid2}/interview")
        check("interview -> 400", r.status_code == 400, r.status_code)
        check("400 文案含「尚未填写资料库」", "尚未填写资料库" in (r.json().get("detail") or ""),
              r.json().get("detail"))
        d2 = client.get(f"/api/chats/{cid2}").json()
        check("快照 name 仍空（未被覆盖）", not (d2.get("profile_snapshot") or {}).get("name"),
              (d2.get("profile_snapshot") or {}).get("name"))
        check("dirty 仍 True", d2.get("profile_dirty") is True, d2.get("profile_dirty"))
        check("无 notice", sync_msgs(client, cid2) == 0, sync_msgs(client, cid2))
        client.delete(f"/api/chats/{cid2}?cascade=true")

        # ---------------- A3 全局也空 ----------------
        print("\n[A3] 空快照 + 全局也空 -> 400 文案不变")
        wipe_global_profile()
        cid3 = client.post("/api/chats", json={}).json()["id"]
        client.post(f"/api/chats/{cid3}/messages", json={"kind": "jd", "content": JD})
        r = client.post(f"/api/chats/{cid3}/interview")
        check("interview -> 400", r.status_code == 400, r.status_code)
        check("400 文案含「尚未填写资料库」", "尚未填写资料库" in (r.json().get("detail") or ""),
              r.json().get("detail"))
        check("无 notice", sync_msgs(client, cid3) == 0, sync_msgs(client, cid3))
        client.delete(f"/api/chats/{cid3}?cascade=true")

        # ---------------- A5 import-url 创建路径 ----------------
        print("\n[A5] import-url 路径：导入时全局空 -> 填全局 -> interview 200 且同步")
        wipe_global_profile()
        orig_fetch, orig_extract = fetchmod.fetch_url_text, fetchmod.extract_jd_from_text

        async def _fake_fetch(u):
            return dict(PAGE)

        async def _fake_extract(text, client_):
            return dict(JD_OUT)

        try:
            fetchmod.fetch_url_text = _fake_fetch
            fetchmod.extract_jd_from_text = _fake_extract
            r = client.post("/api/chats/import-url", json={"url": "https://qa.example.com/job/7"})
            check("import-url -> 201", r.status_code == 201, (r.status_code, r.text[:200]))
            cid5 = r.json()["id"]
            check("导入时全局为空 -> 快照 name 为空",
                  not (r.json().get("profile_snapshot") or {}).get("name"),
                  (r.json().get("profile_snapshot") or {}).get("name"))
        finally:
            fetchmod.fetch_url_text = orig_fetch
            fetchmod.extract_jd_from_text = orig_extract
        client.put("/api/profile", json=PROFILE)
        r = client.post(f"/api/chats/{cid5}/interview")
        check("interview -> 200", r.status_code == 200, r.status_code)
        d5 = client.get(f"/api/chats/{cid5}").json()
        check("快照已同步（name=李四）", (d5.get("profile_snapshot") or {}).get("name") == "李四",
              (d5.get("profile_snapshot") or {}).get("name"))
        check("dirty 仍 False", d5.get("profile_dirty") is False, d5.get("profile_dirty"))
        check("notice 恰好 1 条", sync_msgs(client, cid5) == 1, sync_msgs(client, cid5))
        client.delete(f"/api/chats/{cid5}?cascade=true")

        # ---------------- A6 run_chat 路径：跳过 interview 直接 generate ----------------
        print("\n[A6] run_chat 路径：不 interview 直接 generate -> succeeded 且同步")
        wipe_global_profile()
        cid6 = client.post("/api/chats", json={}).json()["id"]
        check("准备：全局空 -> 快照为空",
              not (client.get(f"/api/chats/{cid6}").json().get("profile_snapshot") or {}).get("name"))
        client.put("/api/profile", json=PROFILE)
        client.post(f"/api/chats/{cid6}/messages", json={"kind": "jd", "content": JD})
        r = client.post(f"/api/chats/{cid6}/generate")
        check("generate -> 202", r.status_code == 202, (r.status_code, r.text[:200]))
        status, err = "", ""
        deadline = time.time() + 40
        while time.time() < deadline:
            d6 = client.get(f"/api/chats/{cid6}").json()
            status, err = d6.get("status", "?"), d6.get("error") or ""
            if status in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.2)
        check("生成 succeeded（mock 模式）", status == "succeeded", (status, err))
        check("error 为空（未报 EMPTY_PROFILE_MSG）", not err, err)
        d6 = client.get(f"/api/chats/{cid6}").json()
        check("快照已同步（name=李四）", (d6.get("profile_snapshot") or {}).get("name") == "李四",
              (d6.get("profile_snapshot") or {}).get("name"))
        check("消息流含 snapshot_sync notice（恰好 1 条）", sync_msgs(client, cid6) == 1,
              sync_msgs(client, cid6))
        client.delete(f"/api/chats/{cid6}?cascade=true")

        # ---------------- A7 语义回归 ----------------
        print("\n[A7a] 非空快照 + dirty=False + 全局后来改了 -> 自动同步为全局最新")
        # （commit f6fb14d 起，未定制快照生成前始终跟随全局；此前旧语义是「只同步空快照」）
        global_a = dict(PROFILE, name="王五", headline="旧定位")
        global_b = dict(PROFILE, name="赵六", headline="新定位")
        wipe_global_profile()
        client.put("/api/profile", json=global_a)
        cid7 = client.post("/api/chats", json={}).json()["id"]
        check("准备：快照拷贝到全局 A（王五）",
              (client.get(f"/api/chats/{cid7}").json().get("profile_snapshot") or {}).get("name") == "王五")
        client.put("/api/profile", json=global_b)   # 全局后来改成 B
        client.post(f"/api/chats/{cid7}/messages", json={"kind": "jd", "content": JD})
        r = client.post(f"/api/chats/{cid7}/interview")
        check("interview -> 200", r.status_code == 200, r.status_code)
        d7 = client.get(f"/api/chats/{cid7}").json()
        check("快照已同步为全局 B（name=赵六）",
              (d7.get("profile_snapshot") or {}).get("name") == "赵六",
              (d7.get("profile_snapshot") or {}).get("name"))
        check("snapshot_sync notice 恰好 1 条", sync_msgs(client, cid7) == 1,
              sync_msgs(client, cid7))
        check("dirty 仍 False", d7.get("profile_dirty") is False, d7.get("profile_dirty"))
        client.delete(f"/api/chats/{cid7}?cascade=true")

        print("\n[A7b] profile/reset 行为不变")
        wipe_global_profile()
        client.put("/api/profile", json=global_a)
        cid8 = client.post("/api/chats", json={}).json()["id"]
        r = client.put(f"/api/chats/{cid8}/profile", json={"name": "定制名"})
        check("chat 内定制快照 -> 200 且 dirty=True",
              r.status_code == 200 and r.json().get("profile_dirty") is True,
              (r.status_code, r.json().get("profile_dirty")))
        check("定制生效（快照 name=定制名）",
              (client.get(f"/api/chats/{cid8}").json().get("profile_snapshot") or {}).get("name") == "定制名")
        r = client.post(f"/api/chats/{cid8}/profile/reset")
        check("reset -> 200", r.status_code == 200, r.status_code)
        d8 = client.get(f"/api/chats/{cid8}").json()
        check("reset 后快照回到全局（name=王五）",
              (d8.get("profile_snapshot") or {}).get("name") == "王五",
              (d8.get("profile_snapshot") or {}).get("name"))
        check("reset 后 dirty=False", d8.get("profile_dirty") is False, d8.get("profile_dirty"))

        print("\n[A7c] dirty 对话在 chat 内改快照仍正常（PUT chat profile）")
        r = client.put(f"/api/chats/{cid8}/profile", json={"name": "再改一次"})
        check("PUT chat profile -> 200", r.status_code == 200, r.status_code)
        d8 = client.get(f"/api/chats/{cid8}").json()
        check("快照更新为「再改一次」且 dirty=True",
              (d8.get("profile_snapshot") or {}).get("name") == "再改一次" and d8.get("profile_dirty") is True,
              ((d8.get("profile_snapshot") or {}).get("name"), d8.get("profile_dirty")))
        client.delete(f"/api/chats/{cid8}?cascade=true")

        # 收尾：清空隔离库里的全局资料，保证可重复运行
        wipe_global_profile()

    print(f"\n===== QA 独立回归：PASS={_PASS} FAIL={_FAIL} =====")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
