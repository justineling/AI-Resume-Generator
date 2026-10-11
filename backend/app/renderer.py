"""动态渲染器：把模型输出的 resume spec 渲染成自包含 HTML。

关键取舍：
- 模型只决定"结构与内容"（spec），HTML 由确定性渲染器产出。
  这样既保留了"版式由模型动态决定"，又保证了 XSS 安全、样式一致、可编辑、可打印。
- 渲染结果是一段自包含 HTML（内联 <style>），因此可以直接放进 iframe 做所见即所得、
  可以单独下载、也可以交给浏览器打印成 PDF。
- 所有用户/模型文本都经过转义（Jinja2 autoescape），AI 二次编辑返回的 HTML 才会被
  当作可信 HTML 处理（这是用户显式发起的编辑动作）。
"""
from __future__ import annotations

import html
from typing import Any

from jinja2 import Environment, select_autoescape

_env = Environment(autoescape=select_autoescape(["html", "xml"]), trim_blocks=True, lstrip_blocks=True)

_RESUME_TEMPLATE = _env.from_string(
    r"""
<div class="resume" data-theme="{{ theme }}" data-layout="{{ layout }}">
  <header class="r-header">
    {% if header.show_avatar and header.avatar %}
    <div class="r-avatar"><img src="{{ header.avatar }}" alt="{{ header.name }} 的头像" /></div>
    {% endif %}
    <div class="r-identity">
      <h1 class="r-name">{{ header.name }}</h1>
      {% if header.headline %}<p class="r-headline">{{ header.headline }}</p>{% endif %}
      {% if header.tagline %}<p class="r-tagline">{{ header.tagline }}</p>{% endif %}
      {% if header.contacts %}
      <ul class="r-contacts">
        {% for c in header.contacts %}
        <li><span class="r-c-label">{{ c.label }}</span><span class="r-c-value">{{ c.value }}</span></li>
        {% endfor %}
      </ul>
      {% endif %}
    </div>
  </header>

  <div class="r-body">
    {% if aside_sections %}
    <aside class="r-col r-col-aside">
      {% for s in aside_sections %}{{ render_section(s, loop.index0 + main_offset) }}{% endfor %}
    </aside>
    {% endif %}
    <main class="r-col r-col-main">
      {% for s in main_sections %}{{ render_section(s, loop.index0) }}{% endfor %}
    </main>
  </div>
</div>
"""
)

_SECTION_MACROS = _env.from_string(
    r"""
{% macro render_section(s, index) %}
<section class="r-section r-type-{{ s.type }} r-emphasis-{{ s.emphasis|default('normal') }}"
         data-section-id="{{ s.id }}" data-section-title="{{ s.title }}" data-section-type="{{ s.type }}">
  <h2 class="r-section-title">
    <span class="r-sec-index">{{ '%02d'|format(index + 1) }}</span>{{ s.title }}
  </h2>
  <div class="r-section-body">
    {% if s.type == 'text' %}
      <p class="r-text">{{ s.content.text }}</p>

    {% elif s.type == 'list' %}
      <ul class="r-list">{% for it in s.content.get('items', []) %}<li>{{ it }}</li>{% endfor %}</ul>

    {% elif s.type == 'experience' %}
      {% for it in s.content.get('items', []) %}
      <article class="r-entry">
        <div class="r-entry-head">
          <h3 class="r-entry-title">{{ it.title }}{% if it.company %} <span class="r-at">@</span> {{ it.company }}{% endif %}</h3>
          <span class="r-entry-period">{{ it.period }}</span>
        </div>
        {% if it.location %}<p class="r-entry-meta">{{ it.location }}</p>{% endif %}
        {% if it.summary %}<p class="r-text">{{ it.summary }}</p>{% endif %}
        {% if it.achievements %}
        <ul class="r-list">{% for a in it.achievements %}<li>{{ a }}</li>{% endfor %}</ul>
        {% endif %}
        {% if it.tech %}
        <ul class="r-tags">{% for t in it.tech %}<li>{{ t }}</li>{% endfor %}</ul>
        {% endif %}
      </article>
      {% endfor %}

    {% elif s.type == 'education' %}
      {% for it in s.content.get('items', []) %}
      <article class="r-entry">
        <div class="r-entry-head">
          <h3 class="r-entry-title">{{ it.school }}</h3>
          <span class="r-entry-period">{{ it.period }}</span>
        </div>
        <p class="r-entry-meta">{{ it.degree }}{% if it.major %} · {{ it.major }}{% endif %}</p>
        {% if it.highlights %}
        <ul class="r-list">{% for h in it.highlights %}<li>{{ h }}</li>{% endfor %}</ul>
        {% endif %}
      </article>
      {% endfor %}

    {% elif s.type == 'projects' %}
      {% for it in s.content.get('items', []) %}
      <article class="r-entry">
        <div class="r-entry-head">
          <h3 class="r-entry-title">{{ it.name }}{% if it.role %} <span class="r-at">·</span> {{ it.role }}{% endif %}</h3>
          <span class="r-entry-period">{{ it.period }}</span>
        </div>
        {% if it.link %}<p class="r-entry-meta">{{ it.link }}</p>{% endif %}
        {% if it.summary %}<p class="r-text">{{ it.summary }}</p>{% endif %}
        {% if it.highlights %}
        <ul class="r-list">{% for h in it.highlights %}<li>{{ h }}</li>{% endfor %}</ul>
        {% endif %}
      </article>
      {% endfor %}

    {% elif s.type == 'skills' %}
      {% for g in s.content.get('groups', []) %}
      <div class="r-skill-group">
        <p class="r-skill-cat">{{ g.category }}</p>
        <ul class="r-tags">
          {% for item in g.get('items', []) %}
          <li><span class="r-skill-name">{{ item.name }}</span>{% if item.level %} <em>{{ item.level }}</em>{% endif %}{% if item.note %}<span class="r-skill-note">{{ item.note }}</span>{% endif %}</li>
          {% endfor %}
        </ul>
      </div>
      {% endfor %}

    {% elif s.type == 'timeline' %}
      <ul class="r-timeline">
        {% for it in s.content.get('items', []) %}
        <li><span class="r-tl-time">{{ it.time }}</span><span class="r-tl-title">{{ it.title }}</span><span class="r-tl-detail">{{ it.detail }}</span></li>
        {% endfor %}
      </ul>

    {% else %}
      {# 未知 type 兜底：尽量把 content 里的 text / items 渲染成可读结构，避免内容静默丢失 #}
      {% if s.content.text %}<p class="r-text">{{ s.content.text }}</p>{% endif %}
      {% if s.content.get('items') %}
      <ul class="r-list">
        {% for it in s.content.get('items', []) %}
        <li>{% if it is mapping %}{{ it.get('title') or it.get('name') or it.get('summary') or it.get('school') or '' }}{% else %}{{ it }}{% endif %}</li>
        {% endfor %}
      </ul>
      {% endif %}
    {% endif %}
  </div>
</section>
{% endmacro %}
"""
)

_render_section = _SECTION_MACROS.module.render_section

# --------------------------------------------------------------------------- #
# 版式 CSS（自包含，随 HTML 一起走，保证下载/打印一致）
# --------------------------------------------------------------------------- #
_BASE_CSS = """
:root{
  --ink:#14181a; --paper:#ffffff; --muted:#6b7378; --rule:#dfe4e6;
  --signal:#0a7ea4; --signal-soft:#eaf6fb; --aside-bg:#f5f8f9;
}
.resume[data-theme="endfield"]{--signal:#8a7a00;--signal-soft:#fdfbe6;--aside-bg:#f7f7f3;}
.resume[data-theme="exa"]{--signal:#0f8f83;--signal-soft:#e9fbf9;--aside-bg:#f4f7f7;}
.resume[data-theme="corporate"]{--signal:#3d4a00;--signal-soft:#f6fbe0;--aside-bg:#f6f6f4;}
.resume[data-tone="dark"]{--ink:#eef2f3;--paper:#111517;--muted:#9aa4a8;--rule:#2a3236;--signal:#4fd2f7;--signal-soft:#12303a;--aside-bg:#171c1f;}

*{box-sizing:border-box;}
.resume{font-family:"Noto Sans SC","Source Han Sans SC","PingFang SC",-apple-system,"Segoe UI",Roboto,sans-serif;
  color:var(--ink); background:var(--paper); font-size:13px; line-height:1.62;
  padding:34px 38px; max-width:820px; margin:0 auto;}
.r-header{display:flex; gap:20px; align-items:flex-start; padding-bottom:16px; border-bottom:2px solid var(--ink);}
.r-avatar{flex:0 0 78px; width:78px; height:78px; overflow:hidden; border:1px solid var(--rule);}
.r-avatar img{width:100%; height:100%; object-fit:cover; display:block;}
.r-identity{flex:1 1 auto; min-width:0;}
.r-name{font-size:30px; line-height:.98; margin:0 0 6px; letter-spacing:-.02em; font-weight:800;}
.r-headline{margin:0; font-size:14px; font-weight:600; color:var(--signal);}
.r-tagline{margin:4px 0 0; font-size:12.5px; color:var(--muted);}
.r-contacts{list-style:none; display:flex; flex-wrap:wrap; gap:4px 16px; margin:10px 0 0; padding:0;}
.r-contacts li{display:flex; gap:6px; align-items:baseline; font-size:11.5px;}
.r-c-label{font-size:9.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted);}
.r-c-value{color:var(--ink);}

.r-body{display:block; margin-top:20px;}
.resume[data-layout="sidebar-left"] .r-body,
.resume[data-layout="two-column"] .r-body{display:grid; grid-template-columns:1fr 236px; gap:26px; align-items:start;}
.resume[data-layout="sidebar-left"] .r-col-aside{order:2;}
.resume[data-layout="sidebar-left"] .r-col-main{order:1;}
.resume[data-layout="two-column"] .r-col-aside{order:1;}
.resume[data-layout="two-column"] .r-col-main{order:2;}

/* timeline：经历/项目条目左侧竖向时间轴（细线 + 方形节点），整体单栏 */
.resume[data-layout="timeline"] .r-entry{position:relative; padding-left:22px;}
.resume[data-layout="timeline"] .r-entry::before{
  content:""; position:absolute; left:5px; top:4px; bottom:-11px; width:2px; background:var(--rule);
}
.resume[data-layout="timeline"] .r-entry:last-child::before{bottom:auto; height:12px;}
.resume[data-layout="timeline"] .r-entry::after{
  content:""; position:absolute; left:1px; top:4px; width:10px; height:10px; background:var(--signal);
}

/* compact-grid：正文区两栏网格，section 直接成为网格项，条目间距压缩 */
.resume[data-layout="compact-grid"] .r-body{display:grid; grid-template-columns:1fr 1fr; gap:14px 22px; align-items:start;}
.resume[data-layout="compact-grid"] .r-col{display:contents;}
.resume[data-layout="compact-grid"] .r-section{margin:0 0 12px;}
.resume[data-layout="compact-grid"] .r-section-title{margin-bottom:6px;}
.resume[data-layout="compact-grid"] .r-entry{margin:0 0 7px;}

.r-section{margin:0 0 18px; break-inside:avoid;}
.r-section-title{font-size:12px; letter-spacing:.12em; text-transform:uppercase; font-weight:800;
  margin:0 0 9px; padding-bottom:4px; border-bottom:1px solid var(--rule); display:flex; gap:8px; align-items:baseline;}
.r-sec-index{font-size:9.5px; color:var(--signal); letter-spacing:.08em;}
.r-emphasis-high .r-section-title{border-bottom-color:var(--signal);}
.r-emphasis-high .r-section-title::after{content:"★"; color:var(--signal); font-size:10px;}

.r-entry{margin:0 0 11px;}
.r-entry-head{display:flex; justify-content:space-between; gap:12px; align-items:baseline;}
.r-entry-title{font-size:13px; font-weight:700; margin:0;}
.r-at{color:var(--signal); font-weight:600;}
.r-entry-period{font-size:11px; color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums;}
.r-entry-meta{margin:2px 0 4px; font-size:11.5px; color:var(--muted);}
.r-text{margin:4px 0;}
.r-list{margin:5px 0; padding-left:16px;}
.r-list li{margin:2px 0;}
.r-tags{list-style:none; display:flex; flex-wrap:wrap; gap:5px; margin:6px 0 0; padding:0;}
.r-tags li{font-size:10.5px; padding:1px 7px; border:1px solid var(--rule); background:var(--signal-soft); color:var(--ink);}
.r-tags li em{color:var(--muted); font-style:normal;}
.r-skill-group{margin-bottom:9px;}
.r-skill-cat{font-size:10px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); margin:0 0 3px;}
/* 技能项：名称 + 熟练度 + 深度线索（note），纵向小卡片排布 */
.r-type-skills .r-tags{gap:6px;}
.r-type-skills .r-tags li{display:flex; flex-direction:column; align-items:flex-start; padding:3px 8px; line-height:1.4;}
.r-skill-name{font-weight:600;}
.r-skill-note{font-size:10px; color:var(--muted); font-weight:400;}
.r-timeline{list-style:none; margin:0; padding:0;}
.r-timeline li{display:grid; grid-template-columns:76px 1fr; gap:2px 10px; padding:5px 0; border-bottom:1px dashed var(--rule);}
.r-tl-time{font-size:11px; color:var(--signal); font-variant-numeric:tabular-nums;}
.r-tl-title{font-weight:600;}
.r-tl-detail{grid-column:2; color:var(--muted); font-size:11.5px;}

/* 打印：A4、保留背景色、去掉外边距 */
@page{size:A4; margin:12mm;}
@media print{
  html,body{margin:0; padding:0; background:#fff;}
  .resume{max-width:none; width:100%; padding:0; font-size:11.5px;}
  .r-section, .r-entry, .r-header{break-inside:avoid;}
  .r-name{font-size:26px;}
}
"""


def _split_columns(sections: list[dict[str, Any]], layout: str) -> tuple[list, list]:
    """按版式把章节分配到侧栏 / 主栏。"""
    aside_types = {"skills", "education", "list"}
    if layout in ("sidebar-left", "two-column"):
        aside = [s for s in sections if s.get("type") in aside_types]
        main = [s for s in sections if s.get("type") not in aside_types]
        # 侧栏为空时退化为单栏，避免出现空洞
        if not aside or not main:
            return [], sections
        return aside, main
    # single-column / timeline / compact-grid：显式走单栏分支
    # （timeline 与 compact-grid 的差异化完全由 data-layout 对应的 CSS 驱动）
    return [], sections


def render_resume_html(spec: dict[str, Any]) -> str:
    """spec -> 自包含 HTML 片段。

    注意：覆盖清单（coverage）是 Resume 的元数据，**不属于简历正文**。渲染器只读取
    theme/layout/header/sections，天然不会渲染 coverage；这里再做一次防御性剔除，
    确保即使调用方误把 coverage 塞进 spec，也不会出现在正文里。
    """
    spec = {k: v for k, v in spec.items() if k not in ("coverage", "coverage_summary")}
    sections = spec.get("sections") or []
    layout = spec.get("layout") or "single-column"
    aside, main = _split_columns(sections, layout)

    header = dict(spec.get("header") or {})
    # 头像在 spec 里只带开关，真实 data-url 由调用方注入
    header.setdefault("show_avatar", False)

    body = _RESUME_TEMPLATE.render(
        theme=spec.get("theme") or "ark",
        layout=layout,
        header=header,
        aside_sections=aside,
        main_sections=main,
        main_offset=len(aside),
        render_section=_render_section,
    )
    return f"<style>{_BASE_CSS}</style>\n{body}"


def build_preview_document(inner_html: str, title: str = "简历") -> str:
    """把简历片段包成一个完整文档，供 iframe / 下载 / 打印使用。

    标题来自岗位名（用户输入），必须转义后再拼进 <title>，否则会形成 XSS。
    所有把 title 拼进 HTML 的入口都应走这里（preview / download / print）。

    额外加一层严格 CSP：简历文档本就不需要执行任何脚本，从浏览器层面禁掉。
    这是"架构级"保证——即便未来净化器被绕过，注入的脚本也跑不起来。
    注意：meta CSP 不支持 frame-ancestors，故不添加（/preview 只在新标签页打开）。
    """
    safe_title = html.escape(title or "简历", quote=True)
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'; "
        "img-src data: https:; base-uri 'none'; form-action 'none'\" />\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{safe_title}</title>\n"
        "</head>\n<body style=\"margin:0;background:#fff;\">\n"
        f"{inner_html}\n"
        "</body>\n</html>"
    )
