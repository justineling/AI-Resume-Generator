"""端到端冒烟测试：资料 -> 批量建任务 -> 并发生成 -> 编辑 -> 导出。

运行： ./.venv/Scripts/python.exe backend/tests/smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from _dbguard import use_isolated_db, warn_if_shared_db  # noqa: E402

# 未显式指定 DATABASE_URL 时自动切到隔离库。
# 必须在 import app.* 之前：app.config.settings 在导入时读取该环境变量。
use_isolated_db("smoke")

from app.main import app  # noqa: E402

PROFILE = {
    "name": "张三",
    "headline": "5 年后端工程师",
    "email": "zhangsan@example.com",
    "phone": "13800000000",
    "location": "上海",
    "summary": "专注高并发后端与云原生，主导过日活百万级系统的架构升级。",
    "skills": [
        {"name": "Python", "level": "精通", "category": "语言"},
        {"name": "Go", "level": "熟练", "category": "语言"},
        {"name": "Kubernetes", "level": "熟练", "category": "工具"},
        {"name": "MySQL", "level": "精通", "category": "存储"},
    ],
    "experiences": [
        {
            "company": "ACME 科技", "title": "高级后端工程师", "start": "2021.03", "end": "至今",
            "location": "上海", "summary": "负责订单核心链路的重构。",
            "achievements": ["把核心接口 P99 从 800ms 降到 120ms", "设计并落地灰度发布体系，故障回滚时间从 30 分钟降到 2 分钟"],
            "tech": ["Python", "Kubernetes", "MySQL"],
        }
    ],
    "education": [
        {"school": "某某大学", "degree": "本科", "major": "计算机科学与技术", "start": "2014", "end": "2018", "highlights": ["GPA 3.7/4.0"]}
    ],
    "projects": [
        {"name": "统一网关", "role": "技术负责人", "start": "2022", "end": "2023", "link": "https://example.com",
         "summary": "为 30+ 内部服务提供统一鉴权与限流。", "highlights": ["支撑 5w QPS"]}
    ],
}

JOBS = [
    "高级后端工程师\n- 3 年以上 Python 后端开发经验\n- 熟悉 Kubernetes / Docker，有云原生落地经验\n- 熟悉 MySQL、Redis，具备高并发系统设计能力\n- 良好的沟通与团队协作能力\nApply Now | Save Job",
    "Go 后端开发工程师\n- 精通 Go，熟悉微服务架构\n- 有 Kubernetes 使用经验\n- 熟悉消息队列 Kafka\n- 具备良好的英文读写能力\n相似职位 | 收藏",
]


def main() -> int:
    warn_if_shared_db("backend/tests/smoke.py")
    with TestClient(app) as client:
        # 1) 保存资料
        r = client.put("/api/profile", json=PROFILE)
        assert r.status_code == 200, r.text
        print("[1] 资料保存 OK")

        # 2) 批量建任务（并发）
        r = client.post("/api/tasks/batch", json={"jobs": [
            {"title": f"岗位{i+1}", "source": "JobStreet", "raw_text": t} for i, t in enumerate(JOBS)
        ]})
        assert r.status_code == 201, r.text
        tasks = r.json()
        ids = [t["id"] for t in tasks]
        print(f"[2] 创建 {len(ids)} 个任务 -> {ids}")

        # 3) 轮询直到完成
        deadline = time.time() + 60
        states: dict[int, str] = {}
        while time.time() < deadline:
            pending = 0
            for tid in ids:
                t = client.get(f"/api/tasks/{tid}").json()
                states[tid] = f"{t['status']}({t['progress']}%) {t['stage']}"
                if t["status"] in ("queued", "running"):
                    pending += 1
            if pending == 0:
                break
            time.sleep(0.4)
        for tid in ids:
            print(f"    task {tid}: {states[tid]}")
        assert all(client.get(f"/api/tasks/{tid}").json()["status"] == "succeeded" for tid in ids), "存在未成功的任务"
        print("[3] 全部任务成功")

        # 4) 取简历
        resume_id = client.get(f"/api/tasks/{ids[0]}").json()["resume_id"]
        res = client.get(f"/api/resumes/{resume_id}").json()
        assert res["html"], "简历 HTML 为空"
        print(f"[4] 简历 #{resume_id} 标题={res['title']!r} theme={res['theme']} 长度={len(res['html'])}")
        print(f"    匹配说明: {res['spec'].get('match_notes')}")

        # 5) AI 二次编辑（mock 模式）
        r = client.post(f"/api/resumes/{resume_id}/ai-edit", json={"instruction": "切换深色"})
        assert r.status_code == 200 and r.json()["ok"], r.text
        print(f"[5] AI 编辑 OK -> {r.json()['summary']}")

        # 6) 改版式（不调 LLM）
        r = client.post(f"/api/resumes/{resume_id}/layout", json={"layout": "single-column"})
        assert r.status_code == 200, r.text
        print(f"[6] 重排版 OK -> layout=single-column, version={r.json()['version']}")

        # 7) 导出
        r = client.get(f"/api/resumes/{resume_id}/download")
        assert r.status_code == 200 and "<!doctype html>" in r.text.lower()
        print(f"[7] 下载 HTML OK ({len(r.text)} bytes)")
        r = client.get(f"/api/resumes/{resume_id}/preview")
        assert r.status_code == 200
        print("[8] 预览文档 OK")

        # 8) 设置
        r = client.get("/api/settings/llm")
        print(f"[9] LLM 设置: mock_mode={r.json()['mock_mode']} worker={r.json()['worker_count']}")

    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
