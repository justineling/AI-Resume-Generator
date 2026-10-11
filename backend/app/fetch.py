"""从招聘网站 URL 抓取并抽取职位信息。

流程：httpx 抓取 HTML → 标准库 HTMLParser 抽正文（去 script/style/nav 等）
→ 优先用 LLM 抽取 {job_title, requirements}，无 key / 解析失败时回落到启发式分节。

安全约束：
- 仅允许 http/https；拒绝 file://、ftp:// 等协议
- 不执行页面 JS（静态抓取）；抓取超时 20s；正文截断防止无上限 prompt
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from .llm import LLMClient, LLMError
from .providers import is_local_url
from .prompts import SYSTEM_EXTRACT_URL

FETCH_TIMEOUT = 20.0
MAX_HTML_BYTES = 2_000_000      # 响应体上限 2MB
MAX_TEXT_CHARS = 20_000         # 送入抽取的正文上限
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """抓取失败（网络 / 协议 / 内容为空）。"""


def validate_url(url: str) -> str:
    """校验并规范化 URL；仅允许 http/https，显式拒绝其它协议（file://、ftp:// 等）。"""
    u = (url or "").strip()
    if not u:
        raise FetchError("请填写招聘页面的链接")
    has_scheme = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u)
    if has_scheme and not re.match(r"^https?://", u, re.I):
        raise FetchError("仅支持 http(s) 网页链接")
    if not has_scheme:
        u = "https://" + u
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("链接格式不正确，请粘贴完整的网页地址（以 http(s):// 开头）")
    return u


# --------------------------------------------------------------------------- #
# 正文抽取（标准库，不引第三方解析器）
# --------------------------------------------------------------------------- #
_SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "template"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "ul", "ol", "h1", "h2", "h3", "h4",
    "h5", "h6", "br", "tr", "table", "header", "footer", "main", "aside",
}


class _TextExtractor(HTMLParser):
    """抽可见文本，跳过 script/style/nav/footer 等非正文区域。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        raw = "\n".join(self._chunks)
        # 压缩空白：多空行合一、行首尾空白去掉
        lines = [ln.strip() for ln in raw.splitlines()]
        lines = [ln for ln in lines if ln]
        return "\n".join(lines)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # 容错：畸形 HTML 尽力抽取
    return parser.text()


async def fetch_url_text(url: str) -> dict[str, Any]:
    """抓取 URL 并返回 {final_url, page_title, text}。失败抛 FetchError。"""
    url = validate_url(url)
    try:
        # 外部地址走系统代理（与 LLM 客户端同策略），本机地址直连
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            trust_env=not is_local_url(url),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException as exc:
        raise FetchError("抓取超时：该网页响应太慢或无法访问") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"无法访问该链接：{exc.__class__.__name__}") from exc

    if resp.status_code >= 400:
        raise FetchError(f"网页返回了错误状态（HTTP {resp.status_code}），请确认链接可直接在浏览器打开")

    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" not in ctype and "text" not in ctype:
        raise FetchError("该链接不是网页（可能是 PDF / 图片），请粘贴招聘详情页的网址")

    html = resp.text[:MAX_HTML_BYTES]
    page_title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        page_title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]

    text = _html_to_text(html)
    if len(text) < 80:
        raise FetchError(
            "未能从该页面提取到有效内容——该网站可能由脚本动态渲染，请改为手动复制职位名称与岗位要求"
        )
    return {"final_url": str(resp.url), "page_title": page_title, "text": text[:MAX_TEXT_CHARS]}


# --------------------------------------------------------------------------- #
# JD 抽取：优先 LLM，回落启发式
# --------------------------------------------------------------------------- #
_SECTION_HEADINGS = (
    "responsibilities", "requirements", "qualifications", "about the role",
    "job description", "description", "working terms", "terms", "benefits",
    "skills", "what you'll do", "what we're looking for", "岗位职责", "任职要求",
    "任职资格", "工作职责", "职位描述", "岗位要求", "福利待遇", "技能要求", "工作内容",
)
_EMPLOYER_Q_HEADINGS = ("employer questions", "application questions", "申请问题", "雇主问题")


def _heuristic_extract(text: str) -> dict[str, str]:
    """启发式：定位 Requirements/任职要求 等小节，截取到下一个已知小节标题为止。"""
    lines = text.splitlines()
    # 找职位标题：前 15 行里最像标题的短行（不含冒号、长度 6-80、非纯小节名）
    job_title = ""
    heading_re = re.compile(r"^\s*(" + "|".join(_SECTION_HEADINGS) + r")\s*:?\s*$", re.I)
    for ln in lines[:15]:
        s = ln.strip().rstrip(":：）) ]")
        if 6 <= len(s) <= 80 and not heading_re.match(ln) and "职位" not in s[:3]:
            job_title = s
            break

    # 找要求小节
    req_start, req_end = -1, len(lines)
    for i, ln in enumerate(lines):
        low = ln.strip().lower().rstrip(":：")
        if req_start < 0 and any(low.startswith(h) for h in
                                 ("requirements", "qualifications", "任职要求", "任职资格", "岗位要求", "job requirements")):
            req_start = i + 1
        elif req_start >= 0 and any(low.startswith(h) for h in
                                    (_SECTION_HEADINGS + _EMPLOYER_Q_HEADINGS)
                                    if not low.startswith(("requirements", "qualifications", "任职要求", "任职资格", "岗位要求"))):
            req_end = i
            break
    requirements = ""
    if req_start >= 0:
        block = [ln.strip("-•· \t") for ln in lines[req_start:req_end] if ln.strip()]
        requirements = "\n".join(b for b in block if len(b) > 2)[:MAX_TEXT_CHARS]
    if not requirements:
        # 兜底：取含动词/关键词密度较高的前 60 行
        requirements = "\n".join(lines[:60])[:MAX_TEXT_CHARS]
    if not job_title:
        job_title = (text.splitlines() or [""])[0].strip()[:80]
    return {"job_title": job_title, "requirements": requirements}


async def extract_jd_from_text(text: str, client: LLMClient | None) -> dict[str, str]:
    """从网页正文抽取职位名称与岗位要求。LLM 优先，失败回落启发式。"""
    if client is not None and client.enabled:
        try:
            out = await client.chat_json(
                SYSTEM_EXTRACT_URL,
                f"以下是招聘网页的正文文本：\n\n<<<PAGE\n{text}\nPAGE>>>",
            )
            jt = str(out.get("job_title") or "").strip()
            rq = str(out.get("requirements") or "").strip()
            if rq:
                return {"job_title": jt[:120], "requirements": rq[:MAX_TEXT_CHARS]}
        except (LLMError, ValueError):
            pass  # 回落启发式
    return _heuristic_extract(text)
