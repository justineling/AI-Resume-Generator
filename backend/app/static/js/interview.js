// AI 问答（flashcard）弹窗：资料页与工作区共用同一套交互。
// 回答存在前端 state 里，来回切卡不丢；允许部分填 / 全空提交。
import { $, el } from "./ui.js";

let dlg = null;
let bound = false;
let busy = false;
let st = null; // 当前会话：{ questions, answers[], index, onSubmit, onSkip, onCancel }

function bind() {
  if (bound) return;
  dlg = $("#interview-dialog");
  bound = true;

  $("#iv-prev").addEventListener("click", () => go(st.index - 1));
  $("#iv-next").addEventListener("click", () => go(st.index + 1));
  $("#iv-skip-card").addEventListener("click", skipCard);
  $("#iv-submit").addEventListener("click", () => run(st.onSubmit, [collect()]));
  $("#iv-skip-all").addEventListener("click", () => run(st.onSkip, []));
  $("#iv-answer").addEventListener("input", () => {
    if (!st) return;
    st.answers[st.index] = $("#iv-answer").value;
    renderDots(); // 圆点即时反映「已作答」
  });
  // Esc = 取消：只关闭弹窗并让调用方收尾，**不提交、不生成**
  // （「跳过，直接生成」是独立的显式按钮，语义不同，不要混用）
  dlg.addEventListener("cancel", (ev) => {
    ev.preventDefault();
    run(st.onCancel, []);
  });
}

// 只回传填了内容的回答；全空时即为「跳过全部」
function collect() {
  return st.questions
    .map((q, i) => ({
      id: q.id || `q${i}`,
      topic: q.topic || "",
      question: q.question || "",
      answer: (st.answers[i] || "").trim(),
    }))
    .filter((a) => a.answer);
}

export function openInterview(opts) {
  bind();
  const questions = Array.isArray(opts.questions) ? opts.questions.slice() : [];
  if (!questions.length) return; // 没有问题就不弹

  st = {
    questions,
    answers: new Array(questions.length).fill(""),
    index: 0,
    onSubmit: opts.onSubmit || null,
    onSkip: opts.onSkip || null,
    onCancel: opts.onCancel || null,
  };
  $("#iv-title").textContent = opts.title || "AI 问答";
  const skipAll = $("#iv-skip-all");
  skipAll.textContent = opts.skipLabel || "跳过";
  skipAll.hidden = !opts.skipLabel;
  renderGaps(opts.gaps || []);
  renderCard();
  renderDots();

  dlg.showModal();
  const ta = $("#iv-answer");
  if (ta) ta.focus();
}

function setBusy(on) {
  busy = on;
  for (const id of ["iv-submit", "iv-skip-all", "iv-prev", "iv-next", "iv-skip-card"]) {
    const b = document.getElementById(id);
    if (b) b.disabled = on;
  }
  $("#iv-submit").textContent = on ? "提交中…" : "确认提交";
}

// 执行回调；成功即关闭，失败则保持弹窗打开（回调负责 toast）
async function run(fn, args) {
  if (busy || !st) return;
  setBusy(true);
  try {
    if (fn) await fn(...args);
    close();
  } catch {
    /* 保持打开以便重试 */
  } finally {
    setBusy(false);
  }
}

function close() {
  if (dlg && dlg.open) dlg.close();
  st = null;
  busy = false;
}

function renderGaps(gaps) {
  const box = $("#iv-gaps");
  box.innerHTML = "";
  const items = (gaps || []).filter(Boolean);
  if (!items.length) { box.hidden = true; return; }
  box.hidden = false;
  box.appendChild(el("p", {
    class: "iv-gaps-lead",
    text: `发现 ${items.length} 处岗位要求在你的资料中暂无依据：`,
  }));
  const ul = el("ul");
  for (const g of items) {
    if (typeof g === "string") { ul.appendChild(el("li", { text: g })); continue; }
    const why = g.why_missing ? ` — ${g.why_missing}` : "";
    ul.appendChild(el("li", { text: `${g.requirement || ""}${why}` }));
  }
  box.appendChild(ul);
}

function renderCard() {
  const q = st.questions[st.index] || {};
  const topic = $("#iv-topic");
  topic.textContent = q.topic || "";
  topic.hidden = !q.topic;
  $("#iv-question").textContent = q.question || "";
  const hint = $("#iv-hint");
  hint.textContent = q.hint || "";
  hint.hidden = !q.hint;

  const ta = $("#iv-answer");
  ta.placeholder = q.placeholder || "写下你的回答（可留空）";
  ta.value = st.answers[st.index] || "";

  $("#iv-prev").disabled = busy || st.index <= 0;
  $("#iv-next").disabled = busy || st.index >= st.questions.length - 1;
  renderProgress();
}

function renderProgress() {
  $("#iv-progress").textContent = `第 ${st.index + 1} / ${st.questions.length} 张`;
}

function renderDots() {
  const host = $("#iv-dots");
  host.innerHTML = "";
  st.questions.forEach((q, i) => {
    const done = !!(st.answers[i] || "").trim();
    host.appendChild(el("button", {
      class: `iv-dot${done ? " is-done" : ""}${i === st.index ? " is-current" : ""}`,
      type: "button",
      "aria-label": `第 ${i + 1} 张：${q.topic || q.question || ""}`.slice(0, 60),
      "aria-current": i === st.index ? "true" : null,
      onclick: () => go(i),
    }));
  });
}

function go(i) {
  if (busy || !st) return;
  const next = Math.max(0, Math.min(st.questions.length - 1, i));
  if (next === st.index) return;
  st.index = next;
  renderCard();
  renderDots();
  const ta = $("#iv-answer");
  if (ta) ta.focus();
}

// 跳过这张：清空当前卡的回答并前进
function skipCard() {
  if (busy || !st) return;
  st.answers[st.index] = "";
  renderDots();
  if (st.index < st.questions.length - 1) go(st.index + 1);
  else $("#iv-answer").value = "";
}
