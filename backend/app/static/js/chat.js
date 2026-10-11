// 02 工作区：对话列表 + 会话面板（消息流 / 生成 / 资料快照 / 雇主问题）
// 单一状态所有者：本模块持有 chats / currentChat / messages，WebSocket 事件在此收敛。
import { api } from "./api.js";
import { $, el, toast, fmtTime, statusLabel } from "./ui.js";
import { openInterview } from "./interview.js";

let chats = new Map();      // id -> chat 摘要（列表接口返回，无 messages）
let currentId = null;       // 当前打开的 chat id
let currentChat = null;     // 当前 chat 详情（含 messages）
let messages = [];          // 当前 chat 的消息（按时间升序）
let onOpenResume = null;    // 「打开简历」回调
let switchView = null;      // 视图切换回调

// composer 两个字段「已持久化」的基准值，用于判断是否需要 PATCH / POST
let savedTitle = "";
let savedReq = "";
// 生成耗时计时
let genStartMs = 0;
let elapsedTimer = null;

const ACTIVE_STATES = new Set(["queued", "running"]);
const DEFAULT_TITLE = "新对话";   // 后端新建对话的默认标题，回填时视为「空」
const COLLAPSE_AT = 480;          // 超过该字符数的气泡默认折叠

// --------------------------------------------------------------------------- //
// 初始化
// --------------------------------------------------------------------------- //
export function initChat({ openResume, switchView: switchTo }) {
  onOpenResume = openResume;
  switchView = switchTo;

  $("#btn-new-chat").addEventListener("click", newChat);
  $("#btn-empty-new-chat").addEventListener("click", newChat);
  $("#chat-list").addEventListener("click", onListClick);

  // ---- 从招聘链接导入 ----
  const importBox = $("#import-url-box");
  const importStatus = $("#import-url-status");
  const showImport = () => {
    importBox.hidden = false;
    importStatus.textContent = "";
    importStatus.className = "test-result";
    $("#import-url-input").value = "";
    $("#import-url-input").focus();
  };
  $("#btn-import-url").addEventListener("click", showImport);
  $("#btn-empty-import-url").addEventListener("click", showImport);
  $("#btn-import-cancel").addEventListener("click", () => { importBox.hidden = true; });
  $("#btn-import-confirm").addEventListener("click", async () => {
    const btn = $("#btn-import-confirm");
    const url = $("#import-url-input").value.trim();
    if (!url) {
      importStatus.className = "test-result is-err";
      importStatus.textContent = "请先粘贴招聘页面的链接";
      return;
    }
    const auto = $("#import-auto-generate").checked;
    btn.disabled = true;
    btn.textContent = "抓取中…";
    importStatus.className = "test-result is-pending";
    importStatus.textContent = "正在抓取网页并识别职位信息…";
    try {
      // 生成一律交给 generateChat()：它会在生成前先做 AI 问答（flashcard），
      // 因此这里固定传 false，避免后端直接入队绕过问答步骤。
      const chat = await api.importFromUrl(url, false);
      importBox.hidden = true;
      await refreshChats();
      await openChat(chat.id);
      if (!chat.raw_text) {
        toast("已创建对话，但未能识别岗位要求，请手动补充", "err");
      } else if (auto) {
        toast("已导入职位信息，正在准备生成前的问题…", "ok");
        await generateChat();   // 与手动点「生成简历」完全同一条路径
      } else {
        toast("已从链接导入职位信息，请核对无误后点「生成简历」", "ok");
      }
    } catch (err) {
      importStatus.className = "test-result is-err";
      importStatus.textContent = `导入失败：${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "开始导入";
    }
  });

  $("#btn-chat-generate").addEventListener("click", generateChat);
  $("#btn-chat-cancel").addEventListener("click", cancelChat);
  $("#btn-chat-retry").addEventListener("click", retryChat);
  $("#btn-chat-delete").addEventListener("click", deleteChat);
  $("#btn-chat-open-resume").addEventListener("click", () => {
    if (currentChat && currentChat.resume_id && onOpenResume) onOpenResume(currentChat.resume_id);
  });

  // composer 两个输入框：变化时刷新按钮可用态；Ctrl/Cmd+Enter 直接生成
  const titleInput = $("#ws-title-input");
  const reqInput = $("#ws-input");
  for (const node of [titleInput, reqInput]) {
    node.addEventListener("input", updateComposerState);
    node.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
        ev.preventDefault();
        generateChat();
      }
    });
  }

  // 窄屏：对话列表折叠为顶部可切换条
  $("#btn-toggle-chats").addEventListener("click", (ev) => {
    const open = $("#workspace").classList.toggle("is-chats-open");
    ev.currentTarget.setAttribute("aria-expanded", String(open));
  });

  // 资料快照
  $("#btn-chat-profile-edit").addEventListener("click", openProfileDialog);
  $("#btn-chat-profile-reset").addEventListener("click", resetProfile);
  $("#btn-chat-profile-save").addEventListener("click", saveProfile);
  $("#btn-chat-profile-close").addEventListener("click", () => $("#chat-profile-dialog").close());
}

// --------------------------------------------------------------------------- //
// 列表
// --------------------------------------------------------------------------- //
export async function refreshChats() {
  try {
    const list = await api.listChats();
    chats = new Map(list.map((c) => [c.id, c]));
    renderList();
    renderBadge();
    // 当前打开的对话若仍存在，静默拉取最新详情（消息 / 状态）
    if (currentId && chats.has(currentId)) {
      await openChat(currentId, { silent: true });
    }
  } catch (err) {
    toast(`载入对话失败：${err.message}`, "err");
  }
}

function renderList() {
  const host = $("#chat-list");
  host.innerHTML = "";
  if (!chats.size) {
    host.appendChild(el("p", { class: "empty", text: "暂无对话，点「＋ 新建」开始。" }));
    return;
  }
  const sorted = Array.from(chats.values()).sort((a, b) => b.id - a.id);
  for (const c of sorted) {
    host.appendChild(el("button", {
      class: `chat-item${c.id === currentId ? " is-active" : ""}`,
      type: "button", dataset: { id: c.id },
    }, [
      el("div", { class: "chat-item-top" }, [
        el("strong", { text: c.title || `对话 #${c.id}` }),
        el("span", { class: "status-chip", dataset: { status: c.status }, text: chatStatusLabel(c.status) }),
      ]),
      el("span", { class: "chat-item-sub", text: `CHAT-${String(c.id).padStart(4, "0")} · ${fmtTime(c.updated_at)}` }),
    ]));
  }
}

function renderBadge() {
  const active = Array.from(chats.values()).filter((c) => ACTIVE_STATES.has(c.status)).length;
  const badge = $("#rail-badge-chats");
  badge.hidden = active === 0;
  badge.textContent = String(active);
}

function onListClick(ev) {
  const item = ev.target.closest(".chat-item");
  if (!item) return;
  const id = Number(item.dataset.id);
  if (id === currentId) return;
  // 切换对话时若 composer 有未保存内容，先确认，避免静默丢弃
  if (composerDirty() && !window.confirm("当前对话的「职位名称 / 岗位要求」有未保存修改，切换后将丢弃。是否继续？")) return;
  openChat(id);
}

// --------------------------------------------------------------------------- //
// 打开 / 渲染会话
// --------------------------------------------------------------------------- //
export async function openChat(id, opts = {}) {
  try {
    const c = await api.getChat(id);
    currentId = id;
    currentChat = c;
    messages = Array.isArray(c.messages) ? c.messages.slice() : [];
    chats.set(id, { ...(chats.get(id) || {}), ...c });

    $("#ws-empty").hidden = true;
    $("#ws-work").hidden = false;
    // 静默刷新（WebSocket / 定时）时不要覆盖用户正在编辑的内容
    if (!opts.silent || !composerDirty()) fillComposer(c);
    renderHead();
    renderStream();
    renderEmployerQuestions();
    renderList();
    if (!opts.silent && switchView) switchView("workspace");
    const stream = $("#ws-stream");
    stream.scrollTop = stream.scrollHeight;
  } catch (err) {
    toast(`打开对话失败：${err.message}`, "err");
  }
}

// 把当前 chat 的 title / raw_text 回填到 composer（默认标题视为空）
function fillComposer(c) {
  savedTitle = (c.title && c.title !== DEFAULT_TITLE) ? c.title : "";
  savedReq = c.raw_text || "";
  $("#ws-title-input").value = savedTitle;
  $("#ws-input").value = savedReq;
  updateComposerState();
}

// composer 内容是否与「已持久化」值不同（用于跳过无变化的保存与切换确认）
function composerDirty() {
  return $("#ws-title-input").value.trim() !== savedTitle
    || $("#ws-input").value.trim() !== savedReq.trim();
}

function renderHead() {
  const c = currentChat;
  if (!c) return;
  $("#ws-code").textContent = `CHAT-${String(c.id).padStart(4, "0")}`;
  $("#ws-title").textContent = c.title || `对话 #${c.id}`;
  setStatus(c.status);

  const active = ACTIVE_STATES.has(c.status);
  // 耗时基准：优先后端 started_at，缺省回退到本地开始时间
  if (active && !genStartMs) genStartMs = c.started_at ? (Date.parse(c.started_at) || Date.now()) : Date.now();
  if (!active) genStartMs = 0;
  ensureElapsedTimer(active);

  $("#btn-chat-cancel").hidden = !active;
  $("#btn-chat-retry").hidden = !(c.status === "failed" || c.status === "cancelled");
  $("#btn-chat-open-resume").hidden = !c.resume_id;

  updateComposerState();
  updateProgress(c);
  renderProfileBar(c);
}

function setStatus(status) {
  const chip = $("#ws-status");
  chip.dataset.status = status;
  chip.textContent = chatStatusLabel(status);
}

function elapsedSeconds() {
  return genStartMs ? Math.max(0, Math.round((Date.now() - genStartMs) / 1000)) : 0;
}

// 生成期间每秒刷新一次「已用时」，结束后自动停表
function ensureElapsedTimer(active) {
  if (active && !elapsedTimer) {
    elapsedTimer = setInterval(() => { if (currentChat) updateProgress(currentChat); }, 1000);
  } else if (!active && elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

// 刷新生成按钮 / 输入框可用态 + 字段提示
function updateComposerState() {
  const c = currentChat;
  const active = !!(c && ACTIVE_STATES.has(c.status));
  const titleVal = $("#ws-title-input").value.trim();
  const reqVal = $("#ws-input").value.trim();

  // 生成按钮始终可见，用文案/禁用态表达当前状态：生成简历 / 保存中… / 生成中… / 重新生成
  const genBtn = $("#btn-chat-generate");
  const busy = genBtn.dataset.busy === "1";
  genBtn.hidden = false;
  genBtn.disabled = active || busy || (!titleVal && !reqVal);
  if (busy) { /* 文案由 generateChat 控制（保存中… / 生成中…） */ }
  else if (active) genBtn.textContent = "生成中…";
  else genBtn.textContent = c && c.resume_id ? "重新生成" : "生成简历";

  // 生成中锁定输入，避免编辑内容与正在跑的任务错位
  $("#ws-title-input").disabled = active;
  $("#ws-input").disabled = active;

  if (active) return;   // 生成中文案由 updateProgress 负责
  const status = $("#ws-composer-status");
  status.classList.remove("is-error", "is-busy");
  if (!titleVal && !reqVal) status.textContent = "填写「职位名称」或「岗位要求」后即可生成。";
  else if (!reqVal) status.textContent = "岗位要求为空：生成前请先粘贴 JD。";
  else status.textContent = "";
}

// 进度条 + 阶段 + 耗时；并同步 composer 旁的状态文案
function updateProgress(c) {
  const box = $("#ws-progress");
  const active = ACTIVE_STATES.has(c.status);
  const progress = c.progress || 0;
  box.hidden = !(active || progress > 0 || c.status === "failed");
  box.dataset.status = c.status || "";
  $("#ws-progress-fill").style.width = `${progress}%`;

  const parts = [`${progress}%`, c.stage || "—"];
  if (active) parts.push(`已用时 ${elapsedSeconds()}s`);
  if (c.error) parts.push(c.error);
  const stage = $("#ws-stage");
  stage.textContent = parts.join(" · ");
  stage.classList.toggle("is-error", !!c.error);

  const status = $("#ws-composer-status");
  if (active) {
    status.textContent = `生成中 · ${c.stage || "处理中"} · 已用时 ${elapsedSeconds()}s`;
    status.classList.add("is-busy");
    status.classList.remove("is-error");
  } else {
    status.classList.remove("is-busy");
    updateComposerState();
  }
}

function renderProfileBar(c) {
  const snap = c.profile_snapshot || {};
  const name = snap.name || "（未填写姓名）";
  const headline = snap.headline ? ` · ${snap.headline}` : "";
  $("#ws-profile-name").textContent = name + headline;
  $("#ws-profile-badge").hidden = !c.profile_dirty;
}

function renderStream() {
  const host = $("#ws-stream");
  host.innerHTML = "";
  if (!messages.length) {
    host.appendChild(el("p", { class: "empty", text: "还没有消息。在下方填写「职位名称」与「岗位要求」后点「生成简历」。未填岗位要求时生成会被拒绝。" }));
    return;
  }
  for (const m of messages) host.appendChild(messageBubble(m));
  host.scrollTop = host.scrollHeight;
}

function messageBubble(m) {
  const kind = m.kind || "text";
  const cls = m.role === "user" ? "ws-bubble-user" : "ws-bubble-assistant";
  const roleLabel = m.role === "user" ? "你" : "系统";
  const kindLabel = { jd: "JD", text: "说明", notice: "提示", guidance: "指引" }[kind] || kind;
  const text = m.content || "";

  const body = el("div", { class: "ws-bubble-body", text });
  const content = el("div", { class: "ws-bubble-content" }, [body]);
  const node = el("div", { class: `ws-bubble ${cls} ws-bubble-${kind}` }, [
    el("span", { class: "ws-bubble-meta", text: `${roleLabel} · ${kindLabel} · ${fmtTime(m.created_at)}` }),
    content,
  ]);

  // 长内容（通常是整段 JD）默认折叠，避免把消息流撑爆
  if (text.length > COLLAPSE_AT) {
    content.classList.add("is-collapsed");
    const toggle = el("button", {
      class: "bubble-toggle", type: "button", text: "展开全文", "aria-expanded": "false",
    });
    toggle.addEventListener("click", () => {
      const collapsed = content.classList.toggle("is-collapsed");
      toggle.textContent = collapsed ? "展开全文" : "收起";
      toggle.setAttribute("aria-expanded", String(!collapsed));
    });
    node.appendChild(toggle);
  }
  return node;
}

function renderEmployerQuestions() {
  const a = (currentChat && currentChat.job_analysis) || {};
  const qs = Array.isArray(a.employer_questions) ? a.employer_questions : [];
  const box = $("#ws-eq");
  const list = $("#ws-eq-list");
  list.innerHTML = "";
  if (!qs.length) { box.hidden = true; return; }
  box.hidden = false;
  $("#ws-eq-count").textContent = String(qs.length);
  for (const q of qs) list.appendChild(el("li", { text: String(q) }));
}

function upsertMessage(msg) {
  if (!msg || msg.id === undefined || msg.id === null) return;
  if (messages.some((m) => m.id === msg.id)) return;
  messages.push(msg);
}

// --------------------------------------------------------------------------- //
// 新建 / 生成 / 取消 / 重试 / 删除
// --------------------------------------------------------------------------- //
async function newChat() {
  // 当前 composer 有未保存内容时，避免静默丢弃
  if (currentId && composerDirty()
      && !window.confirm("当前对话的「职位名称 / 岗位要求」有未保存修改，新建对话将丢弃。是否继续？")) return;
  try {
    const c = await api.createChat({});
    chats.set(c.id, c);
    renderList();
    await openChat(c.id);
    const input = $("#ws-title-input");
    if (input) input.focus();
    toast("已新建对话，填写「职位名称」与「岗位要求」后点「生成简历」", "ok");
  } catch (err) {
    toast(`新建对话失败：${err.message}`, "err");
  }
}

// 生成主流程：保存有变化的字段 → 生成前 AI 问答（有缺口才弹）→ 触发生成
async function generateChat() {
  if (!currentId) return;
  const c = currentChat;
  if (c && ACTIVE_STATES.has(c.status)) { toast("正在生成中，请稍候", "warn"); return; }

  const titleVal = $("#ws-title-input").value.trim();
  const reqVal = $("#ws-input").value.trim();
  const status = $("#ws-composer-status");

  if (!titleVal && !reqVal) { toast("请先填写「职位名称」或「岗位要求」", "warn"); return; }
  if (!reqVal) {
    // 只有岗位要求为空：给出可读提示（后端也会返回 400）
    status.textContent = "岗位要求为空：请先粘贴 JD 再生成。";
    status.classList.add("is-error");
    toast("岗位要求为空：请先粘贴 JD 再生成", "warn", 5000);
    return;
  }

  const btn = $("#btn-chat-generate");
  // 统一解除 busy：清掉标记后由 updateComposerState 依据真实状态收尾，避免按钮卡在 loading
  const finishBusy = () => { delete btn.dataset.busy; updateComposerState(); };

  btn.dataset.busy = "1";
  btn.disabled = true;
  btn.textContent = "保存中…";
  status.classList.remove("is-error");

  // 1) 保存有变化的字段（职位名称 PATCH / 岗位要求 POST）
  try {
    if (titleVal !== savedTitle) {
      const updated = await api.patchChat(currentId, { title: titleVal });
      savedTitle = titleVal;
      currentChat = { ...currentChat, ...updated };
      chats.set(currentId, { ...(chats.get(currentId) || {}), ...updated });
      renderList();
      $("#ws-title").textContent = updated.title || `对话 #${currentId}`;
    }
    if (reqVal !== savedReq.trim()) {
      const msg = await api.postChatMessage(currentId, { kind: "jd", content: reqVal });
      savedReq = reqVal;
      upsertMessage(msg);
      renderStream();
    }
  } catch (err) {
    finishBusy();
    reportGenerateError(err, status);
    return;
  }

  // 2) 生成前先问「岗位需要但资料缺依据」的点
  btn.textContent = "准备问答…";
  let iv;
  try {
    iv = await api.chatInterview(currentId);
  } catch (err) {
    finishBusy();
    reportGenerateError(err, status);
    return;
  }

  const questions = (iv && Array.isArray(iv.questions)) ? iv.questions : [];
  const gaps = (iv && Array.isArray(iv.gaps)) ? iv.gaps : [];

  // 无缺口 -> 直接生成
  if (!questions.length) {
    await startGenerate(btn, status, finishBusy);
    return;
  }

  // 3) 有缺口 -> 弹 flashcard（保持 busy，按钮不卡在 loading）
  btn.textContent = "等待回答…";
  status.classList.add("is-busy");
  status.textContent = `生成前先补充：${questions.length} 个问题待回答`;
  openInterview({
    title: "生成前补充资料",
    questions,
    gaps,
    skipLabel: "跳过，直接生成",
    onSubmit: (answers) => submitInterview(answers, btn, status, finishBusy),
    onSkip: () => submitInterview([], btn, status, finishBusy),
    // Esc / 关闭：取消本次生成，恢复按钮（用户可稍后再点「生成简历」）
    onCancel: () => {
      finishBusy();
      status.classList.remove("is-busy");
      status.textContent = "已取消补充，未开始生成。";
    },
  });
}

// 提交问答（auto_generate=true）并进入生成态
async function submitInterview(answers, btn, status, finishBusy) {
  let out;
  try {
    out = await api.chatInterviewSubmit(currentId, answers, true);
  } catch (err) {
    reportGenerateError(err, status);
    throw err; // 交给弹窗保持打开，便于重试 / 跳过
  }
  currentChat = { ...currentChat, ...out };
  chats.set(currentId, { ...(chats.get(currentId) || {}), ...out });
  genStartMs = 0;   // 让 renderHead 依据 started_at / 当前时间重新起表
  finishBusy();
  renderList();
  renderHead();
  toast(answers.length ? "已提交补充并开始生成…" : "已跳过补充，开始生成…", "ok");
}

// 直接触发后端生成（无缺口时）
async function startGenerate(btn, status, finishBusy) {
  btn.textContent = "生成中…";
  let out;
  try {
    out = await api.generateChat(currentId);
  } catch (err) {
    finishBusy();
    reportGenerateError(err, status);
    return;
  }
  currentChat = { ...currentChat, ...out };
  chats.set(currentId, { ...(chats.get(currentId) || {}), ...out });
  genStartMs = 0;
  finishBusy();
  renderList();
  renderHead();
  toast("已加入队列，正在生成…", "ok");
}

// 生成链路的错误提示（400 缺岗位要求 / 409 正在生成 / 其它）
function reportGenerateError(err, status) {
  if (err.status === 400) {
    status.textContent = err.message;
    status.classList.add("is-error");
    toast(err.message, "warn", 5000);
  } else if (err.status === 409) {
    toast("正在生成中，请稍候", "warn");
  } else {
    status.textContent = `生成失败：${err.message}`;
    status.classList.add("is-error");
    toast(`生成失败：${err.message}`, "err");
  }
}

async function cancelChat() {
  if (!currentId) return;
  const btn = $("#btn-chat-cancel");
  btn.disabled = true;
  try {
    const out = await api.cancelChat(currentId);
    currentChat = { ...currentChat, ...out };
    chats.set(currentId, { ...(chats.get(currentId) || {}), ...out });
    renderHead();
    renderList();
    toast("已请求取消", "warn");
  } catch (err) {
    toast(`取消失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function retryChat() {
  if (!currentId) return;
  const btn = $("#btn-chat-retry");
  btn.disabled = true;
  btn.textContent = "重试中…";
  try {
    const out = await api.retryChat(currentId);
    currentChat = { ...currentChat, ...out };
    chats.set(currentId, { ...(chats.get(currentId) || {}), ...out });
    genStartMs = 0;
    renderHead();
    renderList();
    toast("已重新排队", "ok");
  } catch (err) {
    if (err.status === 409) toast("正在运行或排队中，无法重试", "warn");
    else toast(`重试失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "重试";
  }
}

async function deleteChat() {
  if (!currentChat) return;
  const label = currentChat.title || `对话 #${currentChat.id}`;
  if (!window.confirm(`确定删除「${label}」吗？会同时删除该对话生成的简历，且不可撤销。`)) return;
  try {
    const removedId = currentChat.id;
    await api.deleteChat(removedId);
    toast(`已删除「${label}」`, "ok");
    chats.delete(removedId);
    currentId = null;
    currentChat = null;
    messages = [];
    savedTitle = "";
    savedReq = "";
    genStartMs = 0;
    ensureElapsedTimer(false);
    $("#ws-title-input").value = "";
    $("#ws-input").value = "";
    $("#ws-work").hidden = true;
    $("#ws-empty").hidden = false;
    renderList();
    renderBadge();
  } catch (err) {
    if (err.status === 400) toast(err.message, "warn", 5000);
    else toast(`删除失败：${err.message}`, "err");
  }
}

// --------------------------------------------------------------------------- //
// 资料快照
// --------------------------------------------------------------------------- //
async function openProfileDialog() {
  if (!currentChat) return;
  const snap = currentChat.profile_snapshot || {};
  $("#cps-name").value = snap.name || "";
  $("#cps-headline").value = snap.headline || "";
  $("#cps-summary").value = snap.summary || "";
  $("#chat-profile-dialog").showModal();
}

async function saveProfile() {
  if (!currentId) return;
  const btn = $("#btn-chat-profile-save");
  btn.disabled = true;
  try {
    // 以服务端快照为基底，仅覆盖身份三字段，保留经历 / 项目 / 技能等其它素材
    const base = await api.getChatProfile(currentId);
    delete base.id;
    delete base.created_at;
    delete base.updated_at;
    const c = await api.saveChatProfile(currentId, {
      ...base,
      name: $("#cps-name").value.trim(),
      headline: $("#cps-headline").value.trim(),
      summary: $("#cps-summary").value.trim(),
    });
    currentChat = { ...currentChat, ...c };
    chats.set(currentId, { ...(chats.get(currentId) || {}), ...c });
    renderHead();
    renderList();
    $("#chat-profile-dialog").close();
    toast("本对话的资料已更新（只影响这个对话）", "ok");
  } catch (err) {
    toast(`保存失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function resetProfile() {
  if (!currentId) return;
  try {
    const c = await api.resetChatProfile(currentId);
    currentChat = { ...currentChat, ...c };
    chats.set(currentId, { ...(chats.get(currentId) || {}), ...c });
    renderHead();
    renderList();
    toast("已恢复为「资料」页里的默认资料", "ok");
  } catch (err) {
    toast(`重置失败：${err.message}`, "err");
  }
}

// --------------------------------------------------------------------------- //
// WebSocket 事件分发（chat_message / chat_progress / chat_updated）
// --------------------------------------------------------------------------- //
export function applyChatEvent(event) {
  const { event: name, payload } = event || {};
  if (!name || !payload) return;

  if (name === "chat_message") {
    if (payload.chat_id !== currentId) return;
    upsertMessage(payload.message);
    renderStream();
    return;
  }

  if (name === "chat_progress" || name === "chat_updated") {
    const id = payload.chat_id;
    if (id === undefined || id === null) return;
    const existing = chats.get(id) || { id, title: `对话 #${id}`, status: "queued", progress: 0 };
    const merged = { ...existing, ...payload, id };
    chats.set(id, merged);
    renderList();
    renderBadge();
    if (id === currentId) {
      currentChat = { ...(currentChat || {}), ...merged };
      renderHead();
      // 生成成功后拉取最新详情（补齐 resume_id 与「已生成」提示消息）
      if (name === "chat_updated" && payload.status === "succeeded") {
        openChat(id, { silent: true });
      }
    }
  }
}

// --------------------------------------------------------------------------- //
// 工具
// --------------------------------------------------------------------------- //
function chatStatusLabel(status) {
  if (status === "draft") return "草稿";
  return statusLabel(status);
}
