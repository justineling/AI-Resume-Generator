"""progress 重置验证脚本。

清空资料库制造"稳定失败"的任务，每 0.2s 采样一次状态变迁并打印。
用于人工确认：重排队（queued）与最终失败（failed）时 progress 均已归零。

运行： ./.venv/Scripts/python.exe backend/tests/progress_probe.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from _dbguard import warn_if_shared_db  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Profile  # noqa: E402

JOB = "高级后端工程师\n- 3 年以上 Python 后端开发经验\n- 熟悉 Kubernetes / Docker"
_RESTORE_SKIP = ("id", "created_at", "updated_at")


def main() -> int:
    warn_if_shared_db("backend/tests/progress_probe.py")
    with TestClient(app) as client:
        saved = client.get("/api/profile").json()

        # 清空资料 -> 任务在 10% 阶段稳定抛错，从而触发重试与最终失败
        with session_scope() as session:
            for p in session.exec(select(Profile)).all():
                session.delete(p)

        tid = client.post(
            "/api/tasks", json={"title": "probe", "raw_text": JOB, "max_retries": 2}
        ).json()["id"]
        print(f"task {tid} 采样（每 0.2s）：")
        print(f"{'t(s)':>5}  {'status':<10} {'progress':>8}  {'attempts':>8}  stage")

        t0 = time.time()
        last = None
        bad: list[str] = []
        deadline = t0 + 30
        while time.time() < deadline:
            t = client.get(f"/api/tasks/{tid}").json()
            snap = (t["status"], t["progress"], t["attempts"], t["stage"])
            if snap != last:
                print(f"{time.time() - t0:5.1f}  {t['status']:<10} {t['progress']:>8}"
                      f"  {t['attempts']:>8}  {t['stage']}")
                last = snap
            if t["status"] == "queued" and t["progress"] != 0:
                bad.append(f"{t['status']}({t['progress']}%)")
            if t["status"] in ("failed", "succeeded", "cancelled"):
                break
            time.sleep(0.2)

        final = client.get(f"/api/tasks/{tid}").json()
        print(f"\n最终：status={final['status']} progress={final['progress']} attempts={final['attempts']}")
        print(f"出现 queued+progress!=0 的次数：{len(bad)}")
        ok = not bad and final["status"] == "failed" and final["progress"] == 0

        # 恢复资料，避免污染共享 DB
        client.put("/api/profile", json={k: v for k, v in saved.items() if k not in _RESTORE_SKIP})

        print("PROBE " + ("PASS ✅" if ok else "FAIL ❌"))
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
