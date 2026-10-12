"""本地降级生成器（mock）。

当没有配置 LLM api_key 时启用，用确定性的启发式规则完成
"岗位解析（分节）-> 匹配 -> spec + 覆盖清单"，
保证整条链路（并发、进度、渲染、编辑、导出）都能真实跑通，而不是返回假数据。

它同时也是一份"没有大模型时该怎么做"的参考实现。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

# 常见技术关键词词表，用于在无 LLM 时做粗粒度抽取
_TECH_HINTS = [
    "python", "java", "javascript", "typescript", "go", "golang", "rust", "c++", "c#",
    "php", "ruby", "kotlin", "swift", "scala", "sql", "mysql", "postgresql", "mongodb",
    "redis", "elasticsearch", "kafka", "rabbitmq", "docker", "kubernetes", "k8s", "aws",
    "azure", "gcp", "linux", "git", "ci/cd", "jenkins", "terraform", "react", "vue",
    "angular", "next.js", "node.js", "django", "flask", "fastapi", "spring", "spring boot",
    "laravel", "pytorch", "tensorflow", "pandas", "numpy", "spark", "hadoop", "flink",
    "rest", "graphql", "grpc", "microservices", "html", "css", "tailwind", "figma",
    "tableau", "power bi", "excel", "machine learning", "deep learning", "nlp", "llm",
]

_SOFT_HINTS = [
    "communication", "teamwork", "leadership", "problem solving", "analytical",
    "collaboration", "ownership", "self-motivated", "沟通", "团队", "协作", "领导",
    "责任心", "抗压", "学习能力", "分析", "主动",
]

# 领域 / 方法论关键词（用于 domain_keywords 抽取；同时为技能 note 提供技术语境）
_DOMAIN_HINTS = [
    "高并发", "高可用", "高吞吐", "低延迟", "分布式", "微服务", "云原生", "事件驱动",
    "领域驱动", "ddd", "可观测性", "性能优化", "容灾", "灰度发布", "限流", "熔断",
    "幂等", "缓存", "消息队列", "容器化", "编排", "持续集成", "持续交付", "ci/cd",
    "devops", "自动化测试", "数据一致性", "数据仓库", "实时计算", "推荐系统", "搜索",
    "支付", "风控", "广告", "电商", "金融", "serverless", "service mesh",
]

_NOISE_HINTS = [
    "apply now", "save job", "similar jobs", "report this job", "cookie", "sign in",
    "login", "share this job", "jobstreet", "indeed", "browse jobs", "post a job",
    "立即申请", "收藏", "相似职位", "登录", "注册",
]

_BULLET_RE = re.compile(r"^\s*(?:[-•*·‣▪]|\d+[.)、])\s*(.+)$")
_SENTENCE_SPLIT = re.compile(r"[。.;；\n]")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.#/]{2,}")

_STOP = {
    "the", "and", "for", "with", "you", "are", "our", "will", "that", "this",
    "have", "from", "your", "who", "not", "all", "any", "can", "able", "job",
    "work", "team", "role", "years", "year", "experience", "must", "should",
}

# 分节标题识别：按顺序匹配（employer_questions 优先，避免 "questions" 被 requirements 抢走）
_SECTION_HEADINGS: list[tuple[tuple[str, ...], str]] = [
    (("employer questions", "employer question", "申请问题", "雇主问题", "申请表问题",
      "questions to answer"), "employer_questions"),
    (("responsibilities", "responsibility", "岗位职责", "工作职责", "主要职责", "职责"), "responsibilities"),
    (("qualifications", "qualification", "任职资格", "资格"), "qualifications"),
    (("requirements", "requirement", "任职要求", "职位要求", "岗位要求", "要求"), "requirements"),
    (("skills", "skill", "技能要求", "技能"), "skills"),
    (("about the role", "about us", "about the team", "关于岗位", "关于我们", "关于团队"), "about"),
    (("job description", "description", "职位描述", "工作内容", "岗位描述"), "description"),
    (("working terms", "working hours", "工作条件", "工作地点", "工作安排", "工作时间"), "working_terms"),
    (("benefits", "benefit", "福利待遇", "福利", "待遇", "我们提供"), "benefits"),
]

# 参与 requirements 的 section kind（其余 kind 不进覆盖清单）
_REQ_KINDS = {"responsibilities", "requirements", "qualifications", "skills", "about", "description"}


def _clean_lines(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _is_noise(line: str) -> bool:
    low = line.lower()
    return any(h in low for h in _NOISE_HINTS)


def _match_heading(line: str) -> str | None:
    """判断一行是否是分节标题；是则返回归一化 kind，否则 None。"""
    if _BULLET_RE.match(line):
        return None
    text = line.strip().rstrip(":：").strip().lower()
    if not text or len(text) > 48:
        return None
    for keys, kind in _SECTION_HEADINGS:
        if any(k in text for k in keys):
            return kind
    return None


def _guess_title(lines: list[str]) -> str:
    for ln in lines:
        if _is_noise(ln) or _match_heading(ln) or _BULLET_RE.match(ln):
            continue
        if 2 <= len(ln) <= 60:
            return ln
    return ""


def analyze_job(raw_text: str) -> dict[str, Any]:
    """启发式岗位解析：分节识别 + 扁平需求列表 + 申请表问题单列。"""
    lines = _clean_lines(raw_text)

    # --- 分节识别 ---
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for ln in lines:
        if _is_noise(ln):
            continue
        kind = _match_heading(ln)
        if kind:
            current = {"heading": ln.strip().rstrip(":："), "kind": kind, "items": []}
            sections.append(current)
            continue
        m = _BULLET_RE.match(ln)
        if m:
            item = m.group(1).strip()
        elif current is not None and len(ln) > 6:
            item = ln.strip()
        else:
            item = ""
        if item and current is not None:
            current["items"].append(item)

    # --- 无分节标题时兜底：按 bullet / 长句切分 ---
    if not sections:
        bullets = [m.group(1).strip() for ln in lines if not _is_noise(ln) and (m := _BULLET_RE.match(ln))]
        if not bullets:
            bullets = [s.strip() for ln in lines if not _is_noise(ln)
                       for s in _SENTENCE_SPLIT.split(ln) if len(s.strip()) > 12]
        sections = [{"heading": "Requirements", "kind": "requirements", "items": bullets}]

    # --- 扁平需求列表（id 供覆盖清单引用）---
    requirements: list[dict[str, Any]] = []
    employer_questions: list[str] = []
    for sec in sections:
        if sec["kind"] == "employer_questions":
            employer_questions.extend([it for it in sec["items"] if it])
            continue
        if sec["kind"] not in _REQ_KINDS:
            continue
        rkind = "responsibility" if sec["kind"] == "responsibilities" else "must_have"
        for it in sec["items"]:
            if len(it) < 4:
                continue
            requirements.append({
                "id": f"R{len(requirements) + 1}",
                "kind": rkind,
                "text": it,
                "section": sec["heading"],
            })

    # --- 关键词 / 技术栈 / 领域词 / 软技能 ---
    low = raw_text.lower()
    tech = [t for t in _TECH_HINTS if t in low]
    domain = [d for d in _DOMAIN_HINTS if d in low]
    soft = [s for s in _SOFT_HINTS if s in low]
    words = _WORD_RE.findall(low)
    freq = Counter(w for w in words if w not in _STOP)
    keywords = list(dict.fromkeys(tech + [w for w, _ in freq.most_common(18)]))[:20]

    must = [r["text"] for r in requirements if r["kind"] == "must_have"][:8]
    if not must:
        must = ["（未能从原文抽取到明确的硬性要求，请在设置中接入 LLM 以获得更准确的分析）"]
    resp = [r["text"] for r in requirements if r["kind"] == "responsibility"][:8]
    nice = [r["text"] for r in requirements if r["kind"] == "nice_to_have"][:8]
    noise = [ln for ln in lines if _is_noise(ln)][:3]

    return {
        "job_title": _guess_title(lines),
        "company": "",
        "seniority": _guess_seniority(low),
        "location": "",
        "sections": sections,
        "requirements": requirements,
        "employer_questions": employer_questions,
        "must_have": must,
        "nice_to_have": nice,
        "responsibilities": resp,
        "keywords": keywords,
        "tech_stack": tech,
        "domain_keywords": domain,
        "soft_skills": soft,
        "culture_signals": [],
        "noise_dropped": noise,
        "confidence": 0.35,
        "_mock": True,
    }


def _guess_seniority(low: str) -> str:
    for k, v in [("intern", "实习"), ("junior", "初级"), ("senior", "高级"),
                 ("lead", "负责人"), ("principal", "专家"), ("manager", "经理")]:
        if k in low:
            return v
    return ""


def _match_score(text: str, keywords: list[str]) -> int:
    low = text.lower()
    return sum(1 for k in keywords if k and k.lower() in low)


# --------------------------------------------------------------------------- #
# 覆盖清单（启发式）
# --------------------------------------------------------------------------- #
def _profile_corpus(profile: dict[str, Any]) -> str:
    return json.dumps(profile, ensure_ascii=False).lower()


def _req_keywords(text: str) -> list[str]:
    """从一条需求里抽取可用于匹配资料的候选关键词。"""
    low = text.lower()
    kws = [t for t in _TECH_HINTS if t in low]
    kws += [w for w in _WORD_RE.findall(low) if w not in _STOP]
    return list(dict.fromkeys(kws))


def _find_evidence_ref(profile: dict[str, Any], hits: list[str]) -> str:
    for i, e in enumerate(profile.get("experiences") or []):
        blob = json.dumps(e, ensure_ascii=False).lower()
        if any(h in blob for h in hits):
            for j, a in enumerate(e.get("achievements") or []):
                if any(h in str(a).lower() for h in hits):
                    return f"experiences[{i}].achievements[{j}]"
            return f"experiences[{i}]"
    for i, s in enumerate(profile.get("skills") or []):
        if any(h in str(s.get("name", "")).lower() for h in hits):
            return f"skills[{i}]"
    for i, p in enumerate(profile.get("projects") or []):
        if any(h in json.dumps(p, ensure_ascii=False).lower() for h in hits):
            return f"projects[{i}]"
    for i, ed in enumerate(profile.get("education") or []):
        if any(h in json.dumps(ed, ensure_ascii=False).lower() for h in hits):
            return f"education[{i}]"
    return "profile"


def _evidence_text(profile: dict[str, Any], hits: list[str]) -> str:
    for e in profile.get("experiences") or []:
        if any(h in json.dumps(e, ensure_ascii=False).lower() for h in hits):
            return f"工作经历·{e.get('company', '')}：{e.get('title', '')}"
    for s in profile.get("skills") or []:
        if any(h in str(s.get("name", "")).lower() for h in hits):
            return f"技能·{s.get('name', '')}"
    for p in profile.get("projects") or []:
        if any(h in json.dumps(p, ensure_ascii=False).lower() for h in hits):
            return f"项目·{p.get('name', '')}"
    return "资料库"


def heuristic_coverage(profile: dict[str, Any], analysis: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按"关键词与资料重合度"判定 grounded / capability，产出覆盖清单。"""
    requirements = list(analysis.get("requirements") or [])
    if not requirements:
        fallback = list(analysis.get("must_have") or []) + list(analysis.get("responsibilities") or [])
        requirements = [{"id": f"R{i + 1}", "kind": "must_have", "text": t, "section": ""}
                        for i, t in enumerate(fallback)]

    corpus = _profile_corpus(profile)
    coverage: list[dict[str, Any]] = []
    grounded = capability = 0
    for r in requirements:
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        hits = [w for w in _req_keywords(text) if w in corpus]
        if hits:
            source = "grounded"
            evidence_ref = _find_evidence_ref(profile, hits)
            resume_evidence = _evidence_text(profile, hits)
            note = ""
            grounded += 1
        else:
            source = "capability"
            evidence_ref = ""
            resume_evidence = ""
            note = "资料中无直接对应经历，按能力层面表述"
            capability += 1
        coverage.append({
            "req_id": str(r.get("id") or f"R{len(coverage) + 1}"),
            "requirement": text,
            "section": str(r.get("section") or ""),
            "resume_evidence": resume_evidence,
            "source": source,
            "evidence_ref": evidence_ref,
            "note": note,
        })
    summary = {
        "total": len(coverage),
        "grounded": grounded,
        "capability": capability,
        "uncovered": 0,
    }
    return coverage, summary


# --------------------------------------------------------------------------- #
# 降级模式的内容丰富化（只重组资料里已有的信息，绝不编造事实）
# --------------------------------------------------------------------------- #
#: 技能名 -> 深度线索（note）。按 token 边界匹配，避免 "go" 命中 "django"。
_SKILL_NOTE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("kubernetes", "k8s"), "容器编排与集群运维，生产环境部署与故障排查"),
    (("docker",), "容器化与镜像构建，多阶段构建保证环境一致性"),
    (("python",), "后端服务开发，注重可维护性与性能"),
    (("java",), "JVM 生态后端开发与性能调优"),
    (("golang", "go"), "高并发服务开发，协程与内存模型"),
    (("typescript", "javascript", "node", "node.js"), "服务端 / 前端工程化开发"),
    (("react", "vue", "angular"), "组件化前端工程与状态管理"),
    (("mysql", "postgresql", "postgres", "oracle", "sql server"), "关系型存储设计，索引与慢查询优化"),
    (("redis",), "缓存与分布式锁，热点数据与穿透 / 击穿防护"),
    (("mongodb",), "文档型存储建模与查询优化"),
    (("kafka",), "异步解耦与消息可靠性保障（顺序 / 幂等 / 积压）"),
    (("rabbitmq",), "消息中间件与异步任务解耦"),
    (("elasticsearch", "es"), "检索与日志分析，倒排索引与聚合"),
    (("spark", "flink", "hadoop"), "大数据批 / 流处理与实时计算"),
    (("pytorch", "tensorflow"), "模型训练与推理工程化"),
    (("nginx",), "反向代理与负载均衡配置"),
    (("linux",), "服务部署、排障与性能分析"),
    (("git",), "协作开发与分支 / CI 流程"),
    (("ci/cd", "jenkins", "gitlab", "github actions"), "持续集成与交付流水线"),
    (("terraform",), "基础设施即代码与云资源编排"),
    (("aws", "azure", "gcp"), "云平台服务使用与架构设计"),
    (("grpc",), "高性能 RPC 与接口契约"),
    (("graphql",), "灵活数据查询接口设计"),
    (("rest", "restful"), "RESTful 接口设计与规范"),
]


def _has_token(text: str, token: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None


def _skill_note(name: str, level: str, category: str) -> str:
    """按技能名启发式生成一句深度线索（note）。只描述技术用途，不编造个人经历。"""
    low = str(name or "").lower()
    for keys, note in _SKILL_NOTE_RULES:
        if any(_has_token(low, k) for k in keys):
            return note
    cat = str(category or "").strip()
    return f"{cat}方向的生产实践与问题排查" if cat else "生产实践与问题排查"


def _expand_achievements(exp: dict[str, Any]) -> list[str]:
    """把一条经历从多角度展开为 3-5 条要点：只用资料里已有的 achievements / tech / summary。

    不编造任何新事实；信息不足时宁可少于 3 条，也不虚构。
    """
    out = [str(a).strip() for a in (exp.get("achievements") or []) if str(a).strip()]
    tech = [str(t).strip() for t in (exp.get("tech") or []) if str(t).strip()]
    summary = str(exp.get("summary") or "").strip()
    # 技术选型角度（来自 tech 字段，真实存在）
    if len(out) < 3 and tech:
        out.append(f"技术栈：以 {' / '.join(tech[:6])} 为核心完成系统构建与迭代")
    # 职责 / 实现角度（来自 summary，真实存在）
    if len(out) < 3 and summary:
        out.append(summary if summary.endswith(("。", ".", "！", "!")) else summary + "。")
    return out[:5]


def _build_tagline(analysis: dict[str, Any], job_title: str) -> str:
    """用岗位解析出的领域词 / 技术栈拼一句技术定位（不编造个人经历）。

    优先用中文领域词（读起来像技术定位），不足 2 个时再用技术栈补足。
    """
    domain = [str(k) for k in (analysis.get("domain_keywords") or []) if str(k).strip()]
    parts = list(dict.fromkeys(domain))[:3]
    if len(parts) < 2:
        tech = [str(k) for k in (analysis.get("tech_stack") or []) if str(k).strip()]
        for t in tech:
            if t not in parts:
                parts.append(t)
            if len(parts) >= 3:
                break
    if parts:
        return " · ".join(parts[:3])
    return f"目标岗位：{job_title}"


def generate_spec(profile: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """启发式生成 resume spec：按关键词重合度重排经历、突出匹配技能，并产出覆盖清单。"""
    keywords = [k for k in (analysis.get("keywords") or []) if k]

    # --- header ---
    contacts: list[dict[str, str]] = []
    for label, key in [("Email", "email"), ("Phone", "phone"), ("Location", "location")]:
        if profile.get(key):
            contacts.append({"label": label, "value": str(profile[key])})
    for link in (profile.get("links") or [])[:3]:
        if isinstance(link, dict) and link.get("url"):
            contacts.append({"label": link.get("label") or "Link", "value": link["url"]})

    job_title = analysis.get("job_title") or "目标岗位"
    header = {
        "name": profile.get("name") or "姓名",
        "headline": profile.get("headline") or job_title,
        "tagline": _build_tagline(analysis, job_title),
        "show_avatar": bool(profile.get("avatar")),
        "contacts": contacts,
    }

    # --- 经历：按关键词重合度排序，匹配的标 high ---
    exps = []
    for e in (profile.get("experiences") or []):
        blob = " ".join(str(e.get(k, "")) for k in ("company", "title", "summary")) + " " + \
               " ".join(map(str, e.get("achievements") or [])) + " " + " ".join(map(str, e.get("tech") or []))
        exps.append((_match_score(blob, keywords), e))
    exps.sort(key=lambda x: x[0], reverse=True)

    exp_items = []
    for score, e in exps:
        exp_items.append({
            "company": e.get("company", ""),
            "title": e.get("title", ""),
            "period": _period(e.get("start"), e.get("end")),
            "location": e.get("location", ""),
            "summary": e.get("summary", ""),
            "achievements": _expand_achievements(e),
            "tech": list(e.get("tech") or []),
        })

    # --- 技能：命中的排前面，并为每项生成深度线索 note ---
    skill_groups: dict[str, list[dict[str, str]]] = {}
    for s in (profile.get("skills") or []):
        name = s.get("name", "")
        cat = s.get("category") or "其他"
        level = s.get("level", "")
        skill_groups.setdefault(cat, []).append({
            "name": name, "level": level, "note": _skill_note(name, level, cat),
        })
    for cat in skill_groups:
        skill_groups[cat].sort(key=lambda it: _match_score(it["name"], keywords), reverse=True)
    matched_skills = [k for k in keywords if any(k.lower() in (s.get("name", "").lower()) for s in (profile.get("skills") or []))]

    # --- 章节组装（顺序由岗位特征决定：有匹配技能则技能前置）---
    sections: list[dict[str, Any]] = []
    if profile.get("summary"):
        sections.append({
            "id": "summary", "type": "text", "title": "个人简介", "emphasis": "normal",
            "content": {"text": profile["summary"]},
        })

    if exp_items:
        sections.append({
            "id": "experience", "type": "experience",
            "title": "工作经历",
            "emphasis": "high" if exps and exps[0][0] > 0 else "normal",
            "content": {"items": exp_items},
        })

    if skill_groups:
        sections.append({
            "id": "skills", "type": "skills",
            "title": "专业技能",
            "emphasis": "high" if matched_skills else "normal",
            "content": {"groups": [{"category": c, "items": it} for c, it in skill_groups.items()]},
        })

    if profile.get("projects"):
        sections.append({
            "id": "projects", "type": "projects", "title": "项目经历", "emphasis": "normal",
            "content": {"items": [{
                "name": p.get("name", ""), "role": p.get("role", ""),
                "period": _period(p.get("start"), p.get("end")), "link": p.get("link", ""),
                "summary": p.get("summary", ""), "highlights": list(p.get("highlights") or []),
            } for p in profile["projects"]]},
        })

    if profile.get("education"):
        sections.append({
            "id": "education", "type": "education", "title": "教育背景", "emphasis": "normal",
            "content": {"items": [{
                "school": ed.get("school", ""), "degree": ed.get("degree", ""),
                "major": ed.get("major", ""), "period": _period(ed.get("start"), ed.get("end")),
                "highlights": list(ed.get("highlights") or []),
            } for ed in profile["education"]]},
        })

    extra_items = []
    for c in (profile.get("certificates") or []):
        extra_items.append(" / ".join(x for x in [c.get("name", ""), c.get("issuer", ""), c.get("date", "")] if x))
    for a in (profile.get("awards") or []):
        extra_items.append(" / ".join(x for x in [a.get("name", ""), a.get("date", "")] if x))
    if extra_items:
        sections.append({
            "id": "extras", "type": "list", "title": "证书与荣誉", "emphasis": "low",
            "content": {"items": extra_items},
        })

    # 顺序：emphasis=high 的章节靠前（保留原相对顺序）
    order = {"high": 0, "normal": 1, "low": 2}
    sections.sort(key=lambda s: order.get(s.get("emphasis", "normal"), 1))

    coverage, summary = heuristic_coverage(profile, analysis)

    return {
        "theme": "ark",
        "layout": "sidebar-left",
        "header": header,
        "sections": sections,
        "match_notes": [
            "（本地降级模式）已按岗位关键词重合度对经历与技能排序。",
            f"识别到关键词 {len(keywords)} 个，命中技能 {len(matched_skills)} 项。",
            f"覆盖清单：共 {summary['total']} 条需求，grounded {summary['grounded']} / capability {summary['capability']}。",
        ],
        "coverage": coverage,
        "summary": summary,
        "_mock": True,
    }


def _period(start: Any, end: Any) -> str:
    s = str(start or "").strip()
    e = str(end or "").strip()
    if s and e:
        return f"{s} - {e}"
    return s or e or ""


def edit_html(instruction: str, html: str, scope: str = "whole", target: str = "") -> dict[str, Any]:
    """无 LLM 时的二次编辑：支持少量关键词指令，否则原样返回并给出提示。"""
    low = instruction.lower()
    new_html = html
    summary = ""
    if "删除" in instruction and ("技能" in instruction or "skills" in low):
        new_html = re.sub(r'<section[^>]*data-section-id="skills".*?</section>', "", html, flags=re.S)
        summary = "已尝试移除「专业技能」章节"
    elif "深色" in instruction or "dark" in low:
        new_html = html.replace('data-theme="ark"', 'data-theme="ark" data-tone="dark"')
        summary = "已切换为深色底"
    if not summary:
        summary = "本地降级模式仅支持有限的编辑指令（如“删除技能章节”“切换深色”）。接入 LLM 后可执行任意自然语言修改。"
    return {"html": new_html, "summary": summary}


# --------------------------------------------------------------------------- #
# 粘贴简历文本 -> 结构化资料（无 LLM 时的启发式）
#
# 目标：把用户粘贴的简历原文尽量抽取成 ProfileIn 形状的 dict；抽不到就留空，
# **绝不编造**公司与数字（与 SYSTEM_PARSE_RESUME 的约束一致）。
# --------------------------------------------------------------------------- #
_RESUME_SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("summary", ("个人简介", "自我评价", "个人总结", "个人概述", "关于我", "求职意向",
                 "summary", "profile summary", "about me", "objective")),
    ("experiences", ("工作经历", "工作经验", "职业经历", "工作履历", "从业经历",
                     "experience", "work experience", "employment history", "employment")),
    ("projects", ("项目经历", "项目经验", "project experience", "projects")),
    ("education", ("教育背景", "教育经历", "学历背景", "education", "educational background")),
    ("skills", ("专业技能", "技能清单", "技能特长", "技能", "technical skills", "core skills", "skills")),
    ("certificates", ("证书", "资格证书", "certifications", "certificates", "licenses")),
    ("awards", ("荣誉奖项", "获奖经历", "荣誉", "获奖", "奖项", "awards", "honors", "honours")),
    ("languages", ("语言能力", "语言水平", "语言", "languages")),
    ("links", ("个人链接", "相关链接", "链接", "个人主页", "links")),
]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:(?:\+?86[\s\-]?)?1[3-9]\d{9})|(?:\+?\d{1,3}[\s\-]?\d[\d\s\-()]{6,}\d)")
_URL_RE = re.compile(r"https?://[^\s，。；、）)】\]]+")
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_DATE_RANGE_RE = re.compile(
    r"((?:19|20)\d{2}[./\-年]?\d{0,2})\s*(?:[-~—–至到]|to)\s*"
    r"((?:19|20)\d{2}[./\-年]?\d{0,2}|至今|目前|present|now|current)",
    re.I,
)
_TITLE_HINTS = ("工程师", "开发", "设计", "经理", "主管", "专家", "总监", "架构师", "负责人",
                "lead", "engineer", "developer", "designer", "manager", "architect", "analyst")
_LINK_LABELS = (("github", "GitHub"), ("linkedin", "LinkedIn"), ("gitlab", "GitLab"),
                ("behance", "Behance"), ("dribbble", "Dribbble"), ("medium", "Medium"))


def _resume_heading(line: str) -> str | None:
    """判断一行是否为简历小节标题；是则返回归一化 kind。"""
    if _BULLET_RE.match(line):
        return None
    norm = line.strip().rstrip(":：").strip().lower()
    if not norm or len(norm) > 24:
        return None
    for kind, keys in _RESUME_SECTIONS:
        for k in keys:
            if norm == k or norm.startswith(k) or norm.endswith(k):
                return kind
    return None


def _label_value(text: str, labels: tuple[str, ...]) -> str:
    for lab in labels:
        m = re.search(re.escape(lab) + r"\s*[:：]\s*([^\n]{1,80})", text, re.I)
        if m:
            return m.group(1).strip()
    return ""


def _blocks(lines: list[str]) -> list[list[str]]:
    """按空行切成条目块（保留块内顺序）。"""
    out: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if ln.strip():
            cur.append(ln.strip())
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _split_by_dates(block: list[str]) -> list[list[str]]:
    """一个块里出现多条日期区间时，按日期行再次切分（无空行的简历常见）。"""
    idxs = [i for i, ln in enumerate(block)
            if _DATE_RANGE_RE.search(ln) and not _BULLET_RE.match(ln)]
    if len(idxs) <= 1:
        return [block]
    groups: list[list[str]] = []
    start = 0
    for k, i in enumerate(idxs):
        if k == 0:
            start = i if i == 0 else 0
            continue
        groups.append(block[start:i])
        start = i
    groups.append(block[start:])
    return [g for g in groups if g]


def _dates(block: list[str]) -> tuple[str, str]:
    joined = " ".join(block)
    for ln in block:
        m = _DATE_RANGE_RE.search(ln)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    found = _YEAR_RE.search(joined)
    if found:
        start = found.group(0)
        rest = _YEAR_RE.search(joined[found.end():])
        return start, (rest.group(0) if rest else "")
    return "", ""


def _strip_leading_date(line: str) -> str:
    """去掉首行开头的日期区间，避免混进公司 / 学校名。"""
    s = line.strip()
    m = _DATE_RANGE_RE.match(s)
    if m:
        cleaned = s[m.end():].strip(" -—–~|｜·\t")
        if cleaned:
            return cleaned
    return s


def _head_parts(head: str) -> tuple[str, str]:
    """从条目首行拆出 (主体1, 主体2)：如 公司 | 职位 / 学校 - 专业。"""
    for sep in ("|", "｜", "—", "–", "·"):
        if sep in head:
            parts = [p.strip() for p in head.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
    parts = [p for p in re.split(r"\s{2,}|\t+", head) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return head.strip(), ""


def _body_lines(block: list[str]) -> tuple[str, list[str]]:
    """块内除首行与日期行外：无项目符号的短句归为概述，其余条目归为要点。"""
    summary = ""
    bullets: list[str] = []
    for ln in block[1:]:
        m = _BULLET_RE.match(ln)
        if m:
            bullets.append(m.group(1).strip())
            continue
        if _DATE_RANGE_RE.search(ln) or _YEAR_RE.search(ln):
            continue
        if not summary and len(ln) <= 80:
            summary = ln.strip()
        else:
            bullets.append(ln.strip())
    return summary, [b for b in bullets if b][:6]


def _parse_resume_entries(lines: list[str], kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for blk in _blocks(lines):
        for group in _split_by_dates(blk):
            head = _strip_leading_date(group[0])
            start, end = _dates(group)
            summary, bullets = _body_lines(group)
            a, b = _head_parts(head)
            if kind == "experiences":
                out.append({"company": a[:120], "title": b[:120], "location": "",
                            "start": start, "end": end, "summary": summary[:400],
                            "achievements": bullets, "tech": []})
            elif kind == "projects":
                out.append({"name": (a or head)[:120], "role": b[:80], "start": start, "end": end,
                            "link": "", "summary": summary[:400], "highlights": bullets})
            elif kind == "education":
                out.append({"school": (a or head)[:120], "degree": "", "major": b[:120],
                            "start": start, "end": end, "highlights": bullets})
            elif kind == "certificates":
                out.append({"name": (a or head)[:120], "issuer": b[:120], "date": (start or end)[:40]})
            elif kind == "awards":
                out.append({"name": (a or head)[:120], "date": (start or end)[:40], "detail": summary[:200]})
    return out


def _parse_skills(lines: list[str]) -> list[dict[str, Any]]:
    tokens: list[str] = []
    for ln in lines:
        m = _BULLET_RE.match(ln)
        text = m.group(1) if m else ln
        tokens += [t.strip() for t in re.split(r"[,，、;；/|｜]", text) if t.strip()]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in tokens:
        t = t.strip()
        low = t.lower()
        if not (1 <= len(t) <= 30) or low in seen:
            continue
        seen.add(low)
        out.append({"name": t, "level": "", "category": ""})
        if len(out) >= 40:
            break
    return out


def _collect_links(text: str, lines: list[str]) -> list[dict[str, Any]]:
    urls = _URL_RE.findall(text) + [ln for ln in lines if ln.startswith("http")]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in urls:
        u = u.strip().rstrip(".,，。；;")
        if not u or u in seen:
            continue
        seen.add(u)
        label = ""
        for key, name in _LINK_LABELS:
            if key in u.lower():
                label = name
                break
        out.append({"label": label, "url": u})
    return out[:8]


def parse_resume_text(raw_text: str) -> dict[str, Any]:
    """启发式：把简历纯文本解析成 ProfileIn 形状的 dict（抽不到就留空）。"""
    text = raw_text or ""
    raw_lines = text.splitlines()

    sections: dict[str, list[str]] = {}
    header: list[str] = []
    current: str | None = None
    for ln in raw_lines:
        kind = _resume_heading(ln)
        if kind:
            current = kind
            sections.setdefault(kind, [])
            # 「技能：Python, Java」这类行内小节：冒号后的内容也归入该小节
            m = re.match(r"^\s*[^:：]{1,24}\s*[:：]\s*(.+)$", ln)
            if m:
                sections[kind].append(m.group(1).strip())
            continue
        if current:
            sections[current].append(ln)
        else:
            header.append(ln)

    # --- 身份字段 ---
    name = _label_value(text, ("姓名", "Name"))
    headline = _label_value(text, ("一句话定位", "求职意向", "目标职位", "期望职位", "意向岗位", "定位"))
    location = _label_value(text, ("所在城市", "现居", "所在地", "城市", "地址", "Location"))
    age_raw = _label_value(text, ("年龄", "Age"))
    age: int | None = None
    if age_raw:
        m = re.search(r"\d{1,2}", age_raw)
        if m:
            age = int(m.group(0))

    header_lines = [ln.strip() for ln in header if ln.strip()]
    if not name and header_lines:
        first = header_lines[0]
        if (len(first) <= 20 and "@" not in first and not _YEAR_RE.search(first)
                and not _resume_heading(first)):
            name = first
    if not headline:
        for ln in header_lines:
            low = ln.lower()
            if any(h in low for h in _TITLE_HINTS) and len(ln) <= 60:
                headline = re.split(r"[|｜]|\s{2,}", ln)[0].strip()
                break

    email = _EMAIL_RE.search(text)
    phone = _PHONE_RE.search(text)

    summary = "\n".join(s for s in (l.strip() for l in sections.get("summary", []))
                        if s and not _resume_heading(s))[:800]
    if not summary:
        # 无小节标题时：把头部里较长的说明性句子当作概述
        long_lines = [l for l in header_lines if len(l) >= 40]
        summary = long_lines[0][:800] if long_lines else ""

    return {
        "name": name[:80],
        "headline": headline[:120],
        "email": email.group(0) if email else "",
        "phone": phone.group(0).strip() if phone else "",
        "location": location[:80],
        "age": age,
        "avatar": "",
        "summary": summary,
        "links": _collect_links(text, sections.get("links", [])),
        "experiences": _parse_resume_entries(sections.get("experiences", []), "experiences"),
        "projects": _parse_resume_entries(sections.get("projects", []), "projects"),
        "skills": _parse_skills(sections.get("skills", [])),
        "education": _parse_resume_entries(sections.get("education", []), "education"),
        "certificates": _parse_resume_entries(sections.get("certificates", []), "certificates"),
        "awards": _parse_resume_entries(sections.get("awards", []), "awards"),
        "languages": [{"name": t, "level": ""}
                      for t in (s.strip() for s in
                                re.split(r"[,，、;；/|｜\n]",
                                         "\n".join(sections.get("languages", []))))
                      if 1 <= len(t) <= 30][:10],
        "extra": {},
    }
