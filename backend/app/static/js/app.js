// 应用入口：视图路由、事件流、设置弹窗
import { api, connectEvents } from "./api.js";
import { $, $$, toast } from "./ui.js";
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
  $("#cfg-model-select").addEventListener("change", syncCustomModelField);
  $("#cfg-model-custom").addEventListener("input", () => setTestResult("", ""));

  // 刷新
  $("#btn-refresh").addEventListener("click", async () => {
    await Promise.all([refreshChats(), refreshResumes()]);
    toast("已刷新", "ok");
  });

  // 简历空状态 -> 去工作区生成
  $("#btn-studio-goto-workspace").addEventListener("click", () => switchView("workspace"));

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
