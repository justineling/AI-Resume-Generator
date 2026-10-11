// 04 简历工作台：所见即所得编辑 / HTML 源码 / AI 二次编辑 / 版式 / 打印导出
import { api } from "./api.js";
import { $, $$, el, toast, fmtTime, escapeHtml } from "./ui.js";

const LAYOUTS = [
  { id: "single-column", name: "单栏经典", desc: "ONE COLUMN", bars: [100] },
  { id: "sidebar-left", name: "左主右辅", desc: "MAIN + ASIDE", bars: [72, 24] },
  { id: "two-column", name: "左侧栏", desc: "ASIDE + MAIN", bars: [24, 72] },
  { id: "timeline", name: "时间轴", desc: "TIMELINE", bars: [100, 100, 100] },
  { id: "compact-grid", name: "紧凑网格", desc: "COMPACT", bars: [48, 48] },
];

const THEMES = ["ark", "endfield", "exa", "corporate"];

let current = null;      // 当前简历对象
let dirty = false;
let layoutPick = { layout: "single-column", theme: "ark" };
let openHandlerView = null;

export function initResume(switchView) {
  openHandlerView = switchView;

  // Tab 切换
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  });

  $("#btn-save-resume").addEventListener("click", saveResume);
  $("#btn-print").addEventListener("click", printResume);
  $("#btn-download").addEventListener("click", downloadResume);
  $("#btn-delete-resume").addEventListener("click", deleteResume);
  $("#btn-apply-source").addEventListener("click", applySource);
  $("#btn-ai-run").addEventListener("click", runAIEdit);
  $("#btn-ai-undo").addEventListener("click", undoEdit);
  $("#btn-apply-layout").addEventListener("click", applyLayout);

  $("#resume-list").addEventListener("click", (ev) => {
    const item = ev.target.closest(".resume-item");
    if (item) openResume(Number(item.dataset.id));
  });

  // 源码框与编辑器双向：切到源码 tab 时同步一次
  renderLayoutOptions();
  bindShortcuts();
}

function bindShortcuts() {
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
      const view = document.querySelector('.view[data-view="resume"]');
      if (view && view.classList.contains("is-active") && current) {
        ev.preventDefault();
        saveResume();
      }
    }
  });
}

export async function refreshResumes() {
  try {
    const list = await api.listResumes();
    renderList(list);
  } catch (err) {
    toast(`载入简历失败：${err.message}`, "err");
  }
}

function renderList(list) {
  const host = $("#resume-list");
  host.innerHTML = "";
  if (!list.length) {
    const box = el("div", { class: "empty-block" }, [
      el("p", { class: "empty-title", text: "还没有简历" }),
      el("p", { class: "empty-desc", text: "简历在「工作区」按岗位生成。新建对话、填写岗位要求并生成后，简历会出现在这里。" }),
    ]);
    const go = el("button", { class: "btn btn-ghost btn-sm", type: "button", text: "去工作区生成" });
    go.addEventListener("click", () => { if (openHandlerView) openHandlerView("workspace"); });
    box.appendChild(go);
    host.appendChild(box);
    return;
  }
  for (const r of list) {
    host.appendChild(el("button", {
      class: `resume-item${current && current.id === r.id ? " is-active" : ""}`,
      type: "button", dataset: { id: r.id },
    }, [
      el("strong", { text: r.title || `简历 #${r.id}` }),
      el("span", { text: `#${r.id} · v${r.version} · ${r.theme} · ${fmtTime(r.updated_at)}` }),
    ]));
  }
}

export async function openResume(id) {
  try {
    const r = await api.getResume(id);
    current = r;
    dirty = false;
    layoutPick = { layout: (r.spec && r.spec.layout) || "single-column", theme: r.theme || "ark" };

    $("#studio-work").hidden = false;
    $("#studio-placeholder").hidden = true;
    $("#rs-title").textContent = r.title || `简历 #${r.id}`;
    $("#rs-meta").textContent = `RESUME-${String(r.id).padStart(4, "0")} · v${r.version} · ${r.theme} · ${fmtTime(r.updated_at)}`;

    setEditorHtml(r.html);
    $("#source-area").value = r.html;
    $("#btn-save-resume").disabled = true;
    setEditState(false);

    renderLayoutOptions();
    await renderAnalysis(r);
    await renderEditHistory(r.id);
    selectTab("wysiwyg");
    if (openHandlerView) openHandlerView("resume");
  } catch (err) {
    toast(`打开简历失败：${err.message}`, "err");
  }
}

// --------------------------- 编辑器 --------------------------- //
function setEditorHtml(fragment) {
  const frame = $("#editor-frame");
  frame.srcdoc = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />` +
    `<style>html,body{margin:0;background:#ffffff;}</style></head><body>${fragment}</body></html>`;
  frame.onload = () => {
    const doc = frame.contentDocument;
    if (!doc) return;
    try { doc.designMode = "on"; } catch { /* 某些浏览器需聚焦后设置 */ }
    doc.body.style.minHeight = "60vh";
    doc.addEventListener("input", () => markDirty());
    doc.addEventListener("keyup", () => markDirty());
  };
}

function getEditorHtml() {
  const doc = $("#editor-frame").contentDocument;
  if (!doc || !doc.body) return current ? current.html : "";
  return doc.body.innerHTML;
}

function markDirty() {
  dirty = true;
  $("#btn-save-resume").disabled = false;
  setEditState(true);
}

function setEditState(isDirty) {
  const node = $("#edit-state");
  node.textContent = isDirty ? "有未保存修改" : "已同步";
  node.classList.toggle("is-dirty", isDirty);
}

function selectTab(name) {
  $$(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("is-active", on);
    t.setAttribute("aria-selected", String(on));
  });
  $$(".tabpanel").forEach((p) => p.classList.toggle("is-active", p.dataset.panel === name));
  if (name === "source") $("#source-area").value = getEditorHtml();
  if (name === "wysiwyg" && $("#source-area").value) { /* 保持 */ }
}

function applySource() {
  const html = $("#source-area").value;
  setEditorHtml(html);
  markDirty();
  toast("源码已应用到编辑器", "ok");
  selectTab("wysiwyg");
}

// --------------------------- 保存 --------------------------- //
async function saveResume() {
  if (!current) return;
  const html = getEditorHtml();
  try {
    const saved = await api.saveResume(current.id, { html });
    current = { ...current, ...saved, html: saved.html };
    dirty = false;
    $("#btn-save-resume").disabled = true;
    setEditState(false);
    $("#rs-meta").textContent = `RESUME-${String(saved.id).padStart(4, "0")} · v${saved.version} · ${saved.theme} · ${fmtTime(saved.updated_at)}`;
    $("#source-area").value = saved.html;
    await refreshResumes();
    toast(`已保存（v${saved.version}）`, "ok");
  } catch (err) {
    toast(`保存失败：${err.message}`, "err");
  }
}

// --------------------------- 打印 / 下载 --------------------------- //
async function printResume() {
  if (!current) return;
  const html = getEditorHtml();
  const w = window.open("", "_blank");
  if (!w) {
    // 弹窗被拦截：先保存，再退化为打开后端的独立打印视图（新标签页里按 Ctrl+P）
    toast("弹窗被拦截，改用独立打印视图", "warn");
    if (dirty) await saveResume();
    window.open(api.previewUrl(current.id), "_blank");
    return;
  }
  w.document.write(
    `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />` +
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; ` +
    `style-src 'unsafe-inline'; img-src data: https:; base-uri 'none'; form-action 'none'" />` +
    `<title>${escapeHtml(current.title || "简历")}</title></head>` +
    `<body style="margin:0;background:#fff;">${html}</body></html>`
  );
  w.document.close();
  w.focus();
  // 等样式与头像加载完再唤起打印
  setTimeout(() => { try { w.print(); } catch { /* 用户可手动 Ctrl+P */ } }, 400);
  toast("已打开打印视图，选择「另存为 PDF」即可导出", "ok", 5000);
}

async function downloadResume() {
  if (!current) return;
  // 若编辑器有未保存修改，先保存，保证下载的是最新版
  if (dirty) await saveResume();
  const a = el("a", { href: api.downloadUrl(current.id), download: "" });
  document.body.appendChild(a); a.click(); a.remove();
  toast("已开始下载 HTML", "ok");
}

// --------------------------- 删除简历 --------------------------- //
async function deleteResume() {
  if (!current) return;
  const label = current.title || `简历 #${current.id}`;
  if (!window.confirm(`确定删除「${label}」吗？删除简历会同时删除对应的对话记录，且不可撤销。`)) return;
  try {
    await api.deleteResume(current.id);
    toast(`已删除「${label}」`, "ok");
    // 清空工作区，回到占位状态
    current = null;
    dirty = false;
    $("#studio-work").hidden = true;
    $("#studio-placeholder").hidden = false;
    await refreshResumes();
  } catch (err) {
    toast(`删除失败：${err.message}`, "err");
  }
}

// --------------------------- AI 二次编辑 --------------------------- //
async function runAIEdit() {
  if (!current) return;
  const instruction = $("#ai-instruction").value.trim();
  if (!instruction) { toast("请先描述你想要的修改", "warn"); return; }

  const scope = $("#ai-scope").value;
  const target = $("#ai-target").value.trim();
  pushBubble("user", instruction);
  $("#ai-instruction").value = "";
  const btn = $("#btn-ai-run");
  btn.disabled = true;
  btn.textContent = "修改中…";

  try {
    const res = await api.aiEdit(current.id, {
      instruction, scope, target, current_html: getEditorHtml(),
    });
    if (!res.ok) {
      pushBubble("err", res.error || "修改失败");
      toast("AI 修改失败", "err");
    } else {
      setEditorHtml(res.html);
      $("#source-area").value = res.html;
      current = { ...current, html: res.html };
      dirty = false;
      $("#btn-save-resume").disabled = true;
      setEditState(false);
      pushBubble("ai", res.summary || "已完成修改");
      toast("AI 已修改，可继续调整或保存", "ok");
      await renderEditHistory(current.id);
      await refreshResumes();
    }
  } catch (err) {
    pushBubble("err", err.message);
    toast(`AI 修改失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "执行修改";
  }
}

async function undoEdit() {
  if (!current) return;
  try {
    const res = await api.undoEdit(current.id);
    setEditorHtml(res.html);
    $("#source-area").value = res.html;
    current = { ...current, html: res.html };
    dirty = false;
    $("#btn-save-resume").disabled = true;
    setEditState(false);
    pushBubble("sys", "已回退到上一次编辑前");
    toast("已回退", "ok");
    await renderEditHistory(current.id);
  } catch (err) {
    toast(`回退失败：${err.message}`, "err");
  }
}

function pushBubble(kind, text) {
  const chat = $("#ai-chat");
  const map = { user: "ai-bubble-user", ai: "ai-bubble-ai", err: "ai-bubble-err", sys: "ai-bubble-sys" };
  const label = { user: "你", ai: "AI", err: "错误", sys: "系统" }[kind] || "";
  chat.appendChild(el("div", { class: `ai-bubble ${map[kind] || ""}` }, [
    el("span", { class: "ai-meta", text: label }),
    document.createTextNode(text),
  ]));
  chat.scrollTop = chat.scrollHeight;
}

async function renderEditHistory(id) {
  try {
    const edits = await api.listEdits(id);
    const chat = $("#ai-chat");
    chat.querySelectorAll("[data-history]").forEach((n) => n.remove());
    if (!edits.length) return;
    const header = el("div", { class: "ai-bubble ai-bubble-sys", dataset: { history: "1" } }, [
      el("span", { class: "ai-meta", text: "历史" }),
      document.createTextNode(`最近 ${edits.length} 次编辑：` + edits.slice(0, 5).map((e) => `${e.ok ? "✓" : "✗"} ${e.instruction}`).join("　|　")),
    ]);
    chat.appendChild(header);
    chat.scrollTop = chat.scrollHeight;
  } catch { /* 忽略 */ }
}

// --------------------------- 版式 --------------------------- //
function renderLayoutOptions() {
  const host = $("#layout-grid");
  host.innerHTML = "";
  for (const l of LAYOUTS) {
    const bars = l.bars.map((w, i) => el("i", {
      class: i === 0 ? "sig" : "", style: `flex:0 0 ${w}%`,
    }));
    host.appendChild(el("button", {
      class: `layout-opt${layoutPick.layout === l.id ? " is-active" : ""}`,
      type: "button", dataset: { layout: l.id },
      onclick: () => { layoutPick.layout = l.id; renderLayoutOptions(); },
    }, [
      el("strong", { text: l.name }),
      el("small", { text: l.desc }),
      el("div", { class: "layout-preview" }, bars),
    ]));
  }
  // 主题选择
  const themeHost = $("#layout-grid");
  const themeWrap = el("div", { class: "layout-opt", style: "cursor:default" }, [
    el("strong", { text: "主题配色" }),
    el("div", { class: "keyword-cloud", style: "margin-top:8px" },
      THEMES.map((t) => el("button", {
        class: `kw${layoutPick.theme === t ? " kw-hit" : ""}`, type: "button",
        onclick: (ev) => { layoutPick.theme = t; renderLayoutOptions(); },
      }, [t]))),
  ]);
  themeHost.appendChild(themeWrap);
}

async function applyLayout() {
  if (!current) return;
  try {
    const saved = await api.changeLayout(current.id, layoutPick);
    current = { ...current, ...saved };
    setEditorHtml(saved.html);
    $("#source-area").value = saved.html;
    $("#rs-meta").textContent = `RESUME-${String(saved.id).padStart(4, "0")} · v${saved.version} · ${saved.theme} · ${fmtTime(saved.updated_at)}`;
    dirty = false;
    $("#btn-save-resume").disabled = true;
    setEditState(false);
    toast(`已切换版式：${layoutPick.layout} / ${layoutPick.theme}`, "ok");
    await refreshResumes();
  } catch (err) {
    toast(`切换版式失败：${err.message}`, "err");
  }
}

// --------------------------- 匹配分析 --------------------------- //
async function renderAnalysis(resume) {
  const host = $("#analysis-body");
  host.innerHTML = "";
  if (!resume.chat_id) {
    host.appendChild(el("p", { class: "empty", text: "该简历没有关联的对话，无匹配分析。" }));
    return;
  }
  let chat = null;
  try { chat = await api.getChat(resume.chat_id); } catch { /* ignore */ }
  const a = (chat && chat.job_analysis) || {};

  // 覆盖清单（生成时落库在 Resume.coverage）：有据可依 / 能力层面 / 未覆盖
  renderCoverage(host, resume.coverage);

  if (a.truncated) {
    host.appendChild(block("提示 / NOTICE", el("p", { class: "hint", text: "岗位原文超过 20000 字符，已截断后解析，匹配分析可能未覆盖全部内容。" })));
  }

  const notes = (resume.spec && resume.spec.match_notes) || [];
  if (notes.length) {
    host.appendChild(block("版式与取舍说明 / MATCH NOTES", el("ul", {}, notes.map((n) => el("li", { text: n })))));
  }

  // 雇主可能问到的问题（不属于简历正文，仅作准备参考）
  const eq = a.employer_questions || [];
  if (eq.length) {
    host.appendChild(block("雇主可能问到的问题 / EMPLOYER QUESTIONS",
      el("ul", {}, eq.map((q) => el("li", { text: String(q) })))));
  }

  // 岗位分节解析结果
  const sections = a.sections || [];
  if (sections.length) {
    const wrap = el("div", {});
    for (const s of sections) {
      wrap.appendChild(el("div", { class: "analysis-sub" }, [
        el("h4", { text: s.heading || s.kind || "分节" }),
        el("ul", {}, (s.items || []).map((it) => el("li", { text: String(it) }))),
      ]));
    }
    host.appendChild(block("岗位分节 / SECTIONS", wrap));
  }

  const keywordList = a.keywords || [];
  if (keywordList.length) {
    const skillNames = collectSkillNames(resume.spec);
    host.appendChild(block("岗位关键词 / KEYWORDS", el("div", { class: "keyword-cloud" },
      keywordList.map((k) => el("span", {
        class: `kw${hit(k, skillNames) ? " kw-hit" : ""}`, text: k,
      })))));
  }

  for (const [label, key] of [["硬性要求 / MUST HAVE", "must_have"], ["主要职责 / RESPONSIBILITIES", "responsibilities"],
    ["加分项 / NICE TO HAVE", "nice_to_have"], ["软技能 / SOFT SKILLS", "soft_skills"]]) {
    const items = a[key] || [];
    if (items.length) host.appendChild(block(label, el("ul", {}, items.map((x) => el("li", { text: x })))));
  }

  const dropped = a.noise_dropped || [];
  if (dropped.length) host.appendChild(block("已忽略的噪音 / DROPPED", el("ul", {}, dropped.map((x) => el("li", { text: x })))));

  const tech = a.tech_stack || [];
  if (tech.length) host.appendChild(block("技术栈 / STACK", el("div", { class: "keyword-cloud" },
    tech.map((t) => el("span", { class: "kw kw-hit", text: t })))));

  if (!host.children.length) host.appendChild(el("p", { class: "empty", text: "该对话没有匹配分析数据。" }));
}

// 覆盖清单：需求 → 简历依据（grounded 须有出处；capability 为能力层面表述）
function renderCoverage(host, coverage) {
  const cov = coverage && Array.isArray(coverage.coverage) ? coverage.coverage : [];
  if (!cov.length) return;
  const s = (coverage && coverage.summary) || {};
  const stats = el("div", { class: "cov-stats" }, [
    covStat("总条目", s.total ?? cov.length, ""),
    covStat("有据可依", s.grounded ?? 0, "is-ok"),
    covStat("能力层面", s.capability ?? 0, "is-signal"),
    covStat("未覆盖", s.uncovered ?? 0, "is-warn"),
  ]);
  const list = el("div", { class: "cov-list" }, cov.map((it) => el("div", { class: "cov-item" }, [
    el("div", { class: "cov-head" }, [
      el("span", {
        class: `cov-badge ${it.source === "grounded" ? "is-grounded" : "is-capability"}`,
        text: it.source === "grounded" ? "有据" : "能力",
      }),
      el("span", { class: "cov-req", text: it.requirement || it.req_id || "" }),
      it.section ? el("span", { class: "cov-section", text: it.section }) : null,
    ]),
    it.resume_evidence ? el("p", { class: "cov-evidence", text: `简历依据：${it.resume_evidence}` }) : null,
    it.evidence_ref ? el("p", { class: "cov-ref", text: `出处：${it.evidence_ref}` }) : null,
    it.note ? el("p", { class: "cov-note", text: it.note }) : null,
  ])));
  host.appendChild(block("覆盖清单 / COVERAGE", el("div", {}, [stats, list])));
}

function covStat(label, value, cls) {
  return el("dl", { class: `stat ${cls}` }, [
    el("dt", { text: label }), el("dd", { text: String(value) }),
  ]);
}

function collectSkillNames(spec) {
  const out = [];
  for (const s of (spec && spec.sections) || []) {
    if (s.type === "skills") {
      for (const g of (s.content && s.content.groups) || []) {
        for (const it of g.items || []) out.push(String(it.name || "").toLowerCase());
      }
    }
  }
  return out;
}

function hit(keyword, names) {
  const k = String(keyword).toLowerCase();
  return names.some((n) => n.includes(k) || k.includes(n));
}

function block(title, body) {
  return el("div", { class: "analysis-block" }, [el("h3", { text: title }), body]);
}
