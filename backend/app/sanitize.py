"""HTML 净化器（基于 nh3 / ammonia）。

用途：AI 二次编辑产物的**存储前净化**。模型返回的 HTML 片段可能包含脚本、
事件处理器或危险协议，而 /preview、/download 会以完整文档同源返回，一旦入库
就会形成存储型 XSS。因此在写库之前先做白名单净化。

设计取舍：
- 用成熟库 **nh3**（Rust ammonia 的 Python 绑定）做白名单净化，而不是手写正则——
  正则做 HTML 净化堵不完（属性用 `/` 分隔、实体编码等变体都会漏）。
- 只处理"简历片段"（<body> 内的内容），不处理完整文档。
- 白名单刻意保留简历排版所需的全部特征：<style>、class、data-*、
  <img src="data:image/...">（头像）、<a href="https://...">；同时放宽一组
  **安全的结构/行内标签**（表格、列表、代码等），提升 AI 二次编辑的表达力。
  放宽标签集不会削弱安全性：nh3 对所有标签统一剥离 on* 事件、危险协议与 <script>，
  放宽只增加"能保留什么"，不增加"能执行什么"。
- `<style>` 的**文本内容** nh3 不做净化，故在其后补一道后处理，剥离 style 块内的
  `@import` 外链 at-rule（避免浏览器发起请求形成 IP/网络探测面）。
- removed 标志由一个独立的轻量探测器给出，仅用于驱动提示文案、不承担安全职责
  （nh3 会做规范化——补 rel、调属性顺序，用 clean != html 判断会几乎每次误报）。

对外接口：sanitize_fragment(html) -> (clean_html, removed)
"""
from __future__ import annotations

import html as _html
import re

import nh3

_ALLOWED_TAGS = {
    # 结构 / 版式（渲染器自身产出）
    "style", "div", "section", "article", "header", "main", "aside",
    "h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "span", "em",
    "strong", "a", "img", "br", "hr",
    # 放宽：安全的结构 / 行内标签（提升 AI 二次编辑表达力，不放宽可执行能力）
    "b", "i", "u", "s", "small", "mark", "sub", "sup", "code", "pre",
    "blockquote", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "figure", "figcaption", "time", "abbr", "cite", "q",
}
_ALLOWED_ATTRS = {
    "*": {"class", "style", "data-section-id", "data-section-title",
          "data-section-type", "data-theme", "data-layout", "data-tone"},
    "img": {"src", "alt"},
    "a": {"href"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}

# 只把 script 放进 clean_content_tags（连内容一起删，避免残留成可见文本）。
# 注意：style 必须保留，且 style 不能同时出现在 tags 与 clean_content_tags，
# 否则 nh3 会直接 panic。
_CLEAN_CONTENT_TAGS = {"script"}

_DANGEROUS_RE = re.compile(
    r"<\s*script|<\s*iframe|<\s*object|<\s*embed|<\s*meta|<\s*link"
    r"|on[a-z]+\s*=|javascript:|vbscript:|data:text/html",
    re.I,
)

# 匹配 <style>...</style> 块，仅在其内部剥离 @import（不全局替换，避免误删正文文本）。
_STYLE_BLOCK_RE = re.compile(r"(<style[^>]*>)(.*?)(</style>)", re.S | re.I)
# 匹配 @import at-rule：要求目标是 url(...) 或带引号的字符串，再吃掉行内剩余
# （含 `@import url(x.css) screen and (...)` 这类媒体查询后缀），直到分号。
# 这样既覆盖带/不带 url()、带/不带引号、带媒体查询的常见写法，又不会误伤
# `content:"@import x";` 这种把 @import 当普通文本的声明。
_AT_IMPORT_RE = re.compile(
    r"@import\s+(?:url\([^)]*\)|[\"'][^\"']*[\"'])[^;]*;", re.I
)


def _looks_dangerous(s: str) -> bool:
    """轻量探测：输入是否疑似含危险内容（仅驱动提示文案，不承担安全职责）。"""
    return bool(_DANGEROUS_RE.search(_html.unescape(s).lower()))


def _strip_imports(html: str) -> tuple[str, bool]:
    """剥离 <style> 块内部的 @import at-rule，返回 (处理结果, 是否命中)。

    仅作用于 style 块内，避免误删正文里恰好出现的 "@import" 字样。
    """
    hit = False

    def repl(m: re.Match[str]) -> str:
        nonlocal hit
        css = m.group(2)
        cleaned = _AT_IMPORT_RE.sub("", css)
        if cleaned != css:
            hit = True
        return m.group(1) + cleaned + m.group(3)

    return _STYLE_BLOCK_RE.sub(repl, html), hit


def sanitize_fragment(html: str) -> tuple[str, bool]:
    """净化一段 HTML 片段，返回 (净化后内容, 是否疑似移除了危险内容)。"""
    if not html:
        return html, False
    removed = _looks_dangerous(html)
    clean = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "data"},
        clean_content_tags=_CLEAN_CONTENT_TAGS,
    )
    # nh3 不净化 <style> 文本内容，故其后单独剥离 @import 外链。
    clean, import_hit = _strip_imports(clean)
    return clean, removed or import_hit
