// 应用入口：视图路由、事件流、设置弹窗
import { api, connectEvents } from "./api.js";
import { $, $$, el, toast } from "./ui.js";
import { initProfile } from "./profile.js";
import { initChat, refreshChats, applyChatEvent } from "./chat.js";
import { initResume, refreshResumes, openResume } from "./resume.js";

let activeView = "workspace";

// 供应商预设缓存（首次打开设置时拉取）
let providers = [];
const CUSTOM_MODEL_VALUE = "__custom__";

function providerById(id) {
  return providers.find((p) => p.id === id) || null;
}

function providerShort(p) {
  if (!p) return "";
  return p.short || p.label || p.id || "";
}

async function ensureProviders() {
  if (providers.length) return providers;
  try {
    providers = (await api.getProviders()) || [];
  } catch {
    providers = [];
  }
  return providers;
}

function switchView(name) {
  activeView = name;
  $$(".rail-item").forEach((btn) => {
    const on = btn.dataset.view === name;
    btn.classList.toggle("is-active", on);
    if (on) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === name));
  if (name === "workspace") refreshChats();
  if (name === "resume") refreshResumes();
  $(".main").scrollTop = 0;
}

// --------------------------- LLM 状态 --------------------------- //
async function paintLLMStatus() {
  try {
    const s = await api.getLLMSettings();
    await ensureProviders();
    const node = $("#llm-status");
    const text = node.querySelector(".top-status-text");
    if (s.mock_mode) {
      node.classList.add("is-mock");
      text.textContent = `LLM // 降级模式 · worker ${s.worker_count}`;
      $("#footer-mode").textContent = "LLM: MOCK (启发式)";
    } else {
      node.classList.remove("is-mock");
      const name = providerShort(providerById(s.provider_id)) || s.provider_id || "自定义";
      text.textContent = `LLM // ${name} · ${s.model} · worker ${s.worker_count}`;
      $("#footer-mode").textContent = `LLM: ${name} · ${s.model}`;
    }
  } catch {
    $("#llm-status").querySelector(".top-status-text").textContent = "LLM // 离线";
  }
}

// --------------------------- 设置弹窗 --------------------------- //
function fillProviderSelect(selectedId) {
  const sel = $("#cfg-provider");
  sel.innerHTML = "";
  for (const p of providers) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.label || p.id;
    sel.appendChild(opt);
  }
  if (selectedId && providers.some((p) => p.id === selectedId)) sel.value = selectedId;
  else if (providers.length) sel.value = providers[0].id;
}

function syncCustomModelField() {
  const isCustom = $("#cfg-model-select").value === CUSTOM_MODEL_VALUE;
  $("#cfg-model-custom-field").hidden = !isCustom;
  if (!isCustom) $("#cfg-model-custom").value = "";
}

function fillModelSelect(models, selected) {
  const sel = $("#cfg-model-select");
  sel.innerHTML = "";
  const list = (Array.isArray(models) ? models : []).filter(Boolean);
  for (const m of list) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  }
  const optCustom = document.createElement("option");
  optCustom.value = CUSTOM_MODEL_VALUE;
  optCustom.textContent = "＋ 自定义模型名…";
  sel.appendChild(optCustom);

  if (selected && list.includes(selected)) {
    sel.value = selected;
  } else if (selected) {
    sel.value = CUSTOM_MODEL_VALUE;
    $("#cfg-model-custom").value = selected;
  } else {
    sel.value = list[0] || CUSTOM_MODEL_VALUE;
  }
  syncCustomModelField();
}

function selectedModel() {
  const v = $("#cfg-model-select").value;
  if (v === CUSTOM_MODEL_VALUE) return $("#cfg-model-custom").value.trim();
  return v;
}

function setTestResult(kind, message) {
  const node = $("#cfg-test-result");
  node.className = "test-result" + (kind ? ` is-${kind}` : "");
  node.textContent = message || "";
}

// 醒目展示「API Key 是否已配置」——避免用户误以为需要重新输入
function paintKeyStatus(s) {
  const node = $("#cfg-key-status");
  if (!node) return;
  if (s.has_key) {
    node.className = "key-status is-ok";
    node.textContent = `✓ API Key 已配置（${s.key_hint}），可直接点「测试连接」或重新生成`;
  } else {
    node.className = "key-status is-missing";
    node.textContent = "尚未配置 API Key——当前使用本地降级模式（不调用大模型）";
  }
}


// --------------------------------------------------------------------------- //
// 颜色模式（深色 / 浅色 / 跟随系统）
// 纯前端偏好，存 localStorage（index.html 的早期脚本会在首帧前同步应用一次）
// --------------------------------------------------------------------------- //
const THEME_KEY = "arg.theme";

function currentTheme() {
  try {
    return localStorage.getItem(THEME_KEY) || "dark";
  } catch (e) {
    return "dark";
  }
}

function systemPrefersLight() {
  return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches);
}

function applyTheme(mode) {
  const m = mode || "dark";
  const eff = m === "auto" ? (systemPrefersLight() ? "light" : "dark") : m;
  document.documentElement.setAttribute("data-theme", eff === "light" ? "light" : "dark");
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.setAttribute("content", eff === "light" ? "light dark" : "dark light");
  try {
    localStorage.setItem(THEME_KEY, m);
  } catch (e) { /* 隐私模式下忽略 */ }
}

// 跟随系统时，系统切换要立即生效
if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const onChange = () => { if (currentTheme() === "auto") applyTheme("auto"); };
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else if (mq.addListener) mq.addListener(onChange);
}

function applyProvider(providerId, opts = {}) {
  const p = providerById(providerId);
  const editable = !!(p && p.editable_base_url);
  const base = $("#cfg-base");
  base.readOnly = !editable;
  if (editable) base.value = opts.baseUrl != null ? opts.baseUrl : (p && p.base_url) || "";
  else base.value = (p && p.base_url) || "";

  const docs = $("#cfg-docs");
  if (p && p.docs_url) {
    docs.href = p.docs_url;
    docs.hidden = false;
  } else {
    docs.hidden = true;
  }

  $("#cfg-note").textContent = (p && p.note) || "";

  const help = $("#cfg-key-help");
  help.innerHTML = "";
  if (p && p.requires_key === false) {
    help.textContent = "该供应商为本地部署，通常无需 API Key。";
  } else if (p && p.key_url) {
    help.appendChild(document.createTextNode("获取 API Key："));
    const a = document.createElement("a");
    a.className = "link";
    a.href = p.key_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = p.key_url;
    help.appendChild(a);
  }

  fillModelSelect((p && p.models) || [], opts.model || null);
  setTestResult("", "");
}

async function openSettings() {
  try {
    const [s] = await Promise.all([api.getLLMSettings(), ensureProviders()]);
    fillProviderSelect(s.provider_id);
    const p = providerById(s.provider_id);
    applyProvider(s.provider_id, {
      baseUrl: p && p.editable_base_url ? s.base_url : null,
      model: s.model,
    });
    // 0 表示「不限制」——必须原样显示为 0，不能被当成空值
    $("#cfg-max-tokens").value = s.max_tokens ?? 0;
    $("#cfg-theme").value = currentTheme();
    $("#cfg-key").value = "";
    $("#cfg-key").placeholder = s.has_key ? "已配置，留空保持不变" : "留空则使用降级模式";
    paintKeyStatus(s);
    setTestResult("", "");
  } catch (err) {
    toast(`读取设置失败：${err.message}`, "err");
  }
  $("#settings-dialog").showModal();
}

async function testConnection() {
  const btn = $("#btn-test");
  const pid = $("#cfg-provider").value;
  const key = $("#cfg-key").value.trim();
  const baseUrl = $("#cfg-base").value.trim();
  const model = selectedModel();
  const prevLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = "测试中…";
  setTestResult("pending", "正在测试连接…");
  try {
    const r = await api.testLLM({
      provider_id: pid,
      base_url: baseUrl || null,
      api_key: key || null,
      model: model || null,
    });
    if (r.ok) {
      setTestResult("ok", r.message);
      const ids = (r.models || []).map((m) => m.id).filter(Boolean);
      if (ids.length) {
        const keep = ids.includes(model) ? model : ids[0];
        fillModelSelect(ids, keep);
      }
    } else {
      setTestResult("err", r.message);
    }
  } catch (err) {
    setTestResult("err", `测试失败：${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prevLabel;
  }
}

async function saveSettings() {
  const keyVal = $("#cfg-key").value.trim();
  const model = selectedModel();
  const maxTokensVal = $("#cfg-max-tokens").value;
  const payload = {
    provider_id: $("#cfg-provider").value,
    base_url: $("#cfg-base").value.trim() || undefined,
    model: model || undefined,
    max_tokens: maxTokensVal === "" ? undefined : Number(maxTokensVal),
  };
  if (keyVal) payload.api_key = keyVal;   // 留空表示保持原值
  try {
    const s = await api.saveLLMSettings(payload);
    $("#settings-dialog").close();
    await paintLLMStatus();
    if (s.mock_mode) {
      toast("已保存（仍为降级模式：未填 API Key）", "ok");
    } else {
      const name = providerShort(providerById(s.provider_id)) || s.provider_id || "自定义";
      toast(`已保存：${name} · ${s.model}`, "ok");
    }
  } catch (err) {
    toast(`保存设置失败：${err.message}`, "err");
  }
}

// --------------------------------------------------------------------------- //
// 命令面板（Ctrl/Cmd + K）
// 在 app.js 内注册命令，避免 chat.js / resume.js 与本模块循环依赖；
// 命令行为统一复用既有入口（DOM 点击 / 视图切换 / openResume）。
// --------------------------------------------------------------------------- //
let cmdAll = [];        // 已登记的全部命令
let cmdItems = [];      // 当前过滤后展示的命令
let cmdIndex = 0;       // 高亮项
let cmdReturnFocus = null;

function clickEl(sel) { const n = $(sel); if (n) n.click(); }

function syncThemeSelect() {
  const sel = $("#cfg-theme");
  if (sel) sel.value = currentTheme();
}

function baseCommands() {
  return [
    { group: "工作区", title: "新建对话", hint: "新建一个岗位场景", run: () => clickEl("#btn-new-chat") },
    { group: "工作区", title: "从链接导入", hint: "粘贴招聘页面链接自动抓取",
      run: () => { switchView("workspace"); clickEl("#btn-import-url"); } },
    { group: "导航", title: "打开 资料", hint: "01 资料库", run: () => switchView("profile") },
    { group: "导航", title: "打开 工作区", hint: "02 对话工作区", run: () => switchView("workspace") },
    { group: "导航", title: "打开 简历", hint: "03 简历工作台", run: () => switchView("resume") },
    { group: "系统", title: "设置（模型配置）", hint: "供应商 / API Key / 模型", run: () => clickEl("#btn-settings") },
    { group: "系统", title: "切换颜色模式：深色", run: () => { applyTheme("dark"); syncThemeSelect(); } },
    { group: "系统", title: "切换颜色模式：浅色", run: () => { applyTheme("light"); syncThemeSelect(); } },
    { group: "系统", title: "切换颜色模式：跟随系统", run: () => { applyTheme("auto"); syncThemeSelect(); } },
    { group: "系统", title: "刷新数据", hint: "重新拉取对话与简历", run: () => clickEl("#btn-refresh") },
  ];
}

async function resumeCommands() {
  try {
    const list = await api.listResumes();
    return (Array.isArray(list) ? list : []).slice(0, 5).map((r) => ({
      group: "简历",
      title: `打开简历：${r.title || `简历 #${r.id}`}`,
      hint: `RESUME-${String(r.id).padStart(4, "0")} · v${r.version}`,
      run: () => openResume(r.id),
    }));
  } catch {
    return [];
  }
}

// 模糊匹配：query 的每个字符按顺序出现在标题里即命中
function fuzzyMatch(text, query) {
  const t = String(text).toLowerCase();
  let i = 0;
  for (const ch of String(query).toLowerCase()) {
    if (ch === " ") continue;
    i = t.indexOf(ch, i);
    if (i === -1) return false;
    i += 1;
  }
  return true;
}

function renderCommands(query) {
  const host = $("#cmd-list");
  const q = (query || "").trim();
  cmdItems = q ? cmdAll.filter((c) => fuzzyMatch(c.title, q)) : cmdAll.slice();
  if (cmdIndex >= cmdItems.length) cmdIndex = 0;
  host.innerHTML = "";
  $("#cmd-empty").hidden = cmdItems.length > 0;
  let lastGroup = null;
  cmdItems.forEach((c, i) => {
    if (c.group !== lastGroup) {
      lastGroup = c.group;
      host.appendChild(el("div", { class: "cmd-group", role: "presentation", text: c.group }));
    }
    const btn = el("button", {
      class: "cmd-item", type: "button", id: `cmd-opt-${i}`,
      role: "option", "aria-selected": String(i === cmdIndex),
    }, [
      el("span", { class: "cmd-item-title", text: c.title }),
      c.hint ? el("span", { class: "cmd-item-hint", text: c.hint }) : null,
    ]);
    btn.addEventListener("mousemove", () => setCmdIndex(i, false));
    btn.addEventListener("click", () => runCmd(i));
    host.appendChild(btn);
  });
  syncActiveDescendant();
}

function setCmdIndex(i, scroll = true) {
  if (!cmdItems.length) return;
  cmdIndex = Math.max(0, Math.min(i, cmdItems.length - 1));
  $$("#cmd-list .cmd-item").forEach((node, idx) => {
    node.setAttribute("aria-selected", String(idx === cmdIndex));
  });
  if (scroll) {
    const active = $(`#cmd-opt-${cmdIndex}`);
    if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest" });
  }
  syncActiveDescendant();
}

function syncActiveDescendant() {
  const input = $("#cmd-input");
  if (!input) return;
  if (cmdItems.length) input.setAttribute("aria-activedescendant", `cmd-opt-${cmdIndex}`);
  else input.removeAttribute("aria-activedescendant");
}

function runCmd(i) {
  const cmd = cmdItems[i];
  closePalette();
  if (cmd && typeof cmd.run === "function") {
    // 先关闭面板再执行，避免在 showModal 的浮层上再叠加浮层产生焦点错乱
    setTimeout(() => {
      try { cmd.run(); }
      catch (err) { toast(`命令执行失败：${err.message}`, "err"); }
    }, 0);
  }
}

async function buildPaletteCommands() {
  cmdAll = baseCommands();
  renderCommands($("#cmd-input").value);
  const extra = await resumeCommands();
  if (!extra.length) return;
  cmdAll = baseCommands().concat(extra);
  if ($("#command-palette").open) renderCommands($("#cmd-input").value);
}

function openPalette() {
  const dlg = $("#command-palette");
  if (dlg.open) return;
  cmdReturnFocus = document.activeElement;
  const input = $("#cmd-input");
  input.value = "";
  cmdIndex = 0;
  buildPaletteCommands();
  dlg.showModal();
  input.focus();
}

function closePalette() {
  const dlg = $("#command-palette");
  if (dlg.open) dlg.close();
}

function initPalette() {
  const dlg = $("#command-palette");
  const input = $("#cmd-input");

  input.addEventListener("input", () => {
    cmdIndex = 0;
    renderCommands(input.value);
  });
  // ↑↓ Enter 仅面板内消费，不冒泡成全局快捷键
  input.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return;
    if (ev.key === "ArrowDown") { ev.preventDefault(); ev.stopPropagation(); setCmdIndex(cmdIndex + 1); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); ev.stopPropagation(); setCmdIndex(cmdIndex - 1); }
    else if (ev.key === "Enter") { ev.preventDefault(); ev.stopPropagation(); runCmd(cmdIndex); }
  });
  // 关闭（Esc / 执行后）时把焦点还原到触发前的元素
  dlg.addEventListener("close", () => {
    cmdItems = [];
    cmdIndex = 0;
    const back = cmdReturnFocus;
    cmdReturnFocus = null;
    if (back && typeof back.focus === "function" && document.contains(back)) back.focus();
  });
}

function isComposing(ev) {
  return !!(ev && (ev.isComposing || ev.keyCode === 229));
}

// 全局快捷键：Ctrl/Cmd+K（命令面板）、Ctrl/Cmd+S（保存，阻止浏览器默认）、Ctrl/Cmd+Enter（主操作）
function initShortcuts() {
  document.addEventListener("keydown", (ev) => {
    const meta = ev.ctrlKey || ev.metaKey;
    const key = (ev.key || "").toLowerCase();

    if (meta && key === "k") {
      ev.preventDefault();
      const dlg = $("#command-palette");
      if (dlg.open) closePalette(); else openPalette();
      return;
    }

    if (meta && key === "s") {
      // 始终阻止浏览器「保存网页」；简历工作台由 resume.js 处理保存
      ev.preventDefault();
      if (activeView === "profile" && !isComposing(ev)) clickEl("#btn-save-profile");
      return;
    }

    if (meta && ev.key === "Enter" && !isComposing(ev)) {
      const ivDlg = $("#interview-dialog");
      if (ivDlg && ivDlg.open) { ev.preventDefault(); clickEl("#iv-submit"); return; }
      // 工作区 Shift/Ctrl+Enter 由 chat.js 在职位名/岗位要求框上处理（避免重复触发）
      if (activeView === "profile") { ev.preventDefault(); clickEl("#btn-save-profile"); return; }
    }
  });
}

// --------------------------- 启动 --------------------------- //
async function boot() {
  // 导航
  $$(".rail-item").forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));

  // 设置
  $("#btn-settings").addEventListener("click", openSettings);
  $("#btn-close-settings").addEventListener("click", () => $("#settings-dialog").close());
  $("#btn-save-settings").addEventListener("click", saveSettings);
  $("#btn-test").addEventListener("click", testConnection);
  $("#cfg-provider").addEventListener("change", () => applyProvider($("#cfg-provider").value, {}));
  // 颜色模式：切换立即生效（不依赖「保存设置」，所见即所得）
  $("#cfg-theme").addEventListener("change", () => applyTheme($("#cfg-theme").value));
  $("#cfg-model-select").addEventListener("change", syncCustomModelField);
  $("#cfg-model-custom").addEventListener("input", () => setTestResult("", ""));

  // 刷新
  $("#btn-refresh").addEventListener("click", async () => {
    await Promise.all([refreshChats(), refreshResumes()]);
    toast("已刷新", "ok");
  });

  // 简历空状态 -> 去工作区生成
  $("#btn-studio-goto-workspace").addEventListener("click", () => switchView("workspace"));

  // 命令面板 + 全局快捷键
  initPalette();
  initShortcuts();

  // 颜色模式兜底应用（index.html 早期脚本已做，此处保证 JS 状态一致）
  applyTheme(currentTheme());

  // 模块初始化
  await initProfile();
  initChat({ openResume: (rid) => openResume(rid), switchView });
  initResume(switchView);

  // 事件流：chat 事件收敛到 chat.js；生成成功后顺带刷新简历列表
  connectEvents((evt) => {
    applyChatEvent(evt);
    if (evt && evt.event === "chat_updated" && evt.payload && evt.payload.status === "succeeded") {
      refreshResumes();
    }
  });

  // 初始数据
  await paintLLMStatus();
  await refreshChats();
  await refreshResumes();

  // 简历列表定时兜底刷新（WebSocket 断线时仍能同步）
  setInterval(() => { if (activeView === "resume") refreshResumes(); }, 15000);
}

boot().catch((err) => {
  console.error(err);
  toast(`初始化失败：${err.message}`, "err", 8000);
});
