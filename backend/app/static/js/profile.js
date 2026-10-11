// 01 资料库：表单 + 可重复条目 + 头像 + 导入导出
import { api } from "./api.js";
import { $, el, toast, fmtTime } from "./ui.js";

// 每个可重复条目的字段定义（type: text | textarea | lines | csv）
const REPEATERS = {
  links: {
    title: "链接", fields: [
      { key: "label", label: "名称", ph: "GitHub" },
      { key: "url", label: "地址", ph: "https://github.com/you" },
    ],
  },
  experiences: {
    title: "工作经历", fields: [
      { key: "company", label: "公司", ph: "ACME 科技" },
      { key: "title", label: "职位", ph: "高级后端工程师" },
      { key: "location", label: "城市", ph: "上海" },
      { key: "start", label: "开始", ph: "2021.03" },
      { key: "end", label: "结束", ph: "至今" },
      { key: "summary", label: "职责概述", type: "textarea", block: true, ph: "一句话说明你负责什么。" },
      { key: "achievements", label: "量化成果（每行一条）", type: "lines", block: true, ph: "把核心接口 P99 从 800ms 降到 120ms" },
      { key: "tech", label: "技术栈（逗号分隔）", type: "csv", block: true, ph: "Python, Kubernetes, MySQL" },
    ],
  },
  skills: {
    title: "技能", fields: [
      { key: "name", label: "技能", ph: "Python" },
      { key: "level", label: "熟练度", ph: "精通 / 熟练 / 了解" },
      { key: "category", label: "分类", ph: "语言 / 框架 / 工具" },
    ],
  },
  projects: {
    title: "项目", fields: [
      { key: "name", label: "项目名", ph: "统一网关" },
      { key: "role", label: "角色", ph: "技术负责人" },
      { key: "start", label: "开始", ph: "2022" },
      { key: "end", label: "结束", ph: "2023" },
      { key: "link", label: "链接", ph: "https://…" },
      { key: "summary", label: "概述", type: "textarea", block: true },
      { key: "highlights", label: "亮点（每行一条）", type: "lines", block: true },
    ],
  },
  education: {
    title: "教育", fields: [
      { key: "school", label: "学校", ph: "某某大学" },
      { key: "degree", label: "学历", ph: "本科" },
      { key: "major", label: "专业", ph: "计算机科学与技术" },
      { key: "start", label: "开始", ph: "2014" },
      { key: "end", label: "结束", ph: "2018" },
      { key: "highlights", label: "亮点（每行一条）", type: "lines", block: true, ph: "GPA 3.7/4.0" },
    ],
  },
  certificates: {
    title: "证书", fields: [
      { key: "name", label: "名称", ph: "AWS 解决方案架构师" },
      { key: "issuer", label: "颁发机构", ph: "Amazon" },
      { key: "date", label: "时间", ph: "2023" },
    ],
  },
  awards: {
    title: "荣誉", fields: [
      { key: "name", label: "名称", ph: "年度最佳员工" },
      { key: "date", label: "时间", ph: "2023" },
      { key: "detail", label: "说明", ph: "" },
    ],
  },
  languages: {
    title: "语言", fields: [
      { key: "name", label: "语言", ph: "英语" },
      { key: "level", label: "水平", ph: "CET-6 / 流利" },
    ],
  },
};

const SCALARS = {
  name: "pf-name", headline: "pf-headline", email: "pf-email", phone: "pf-phone",
  location: "pf-location", age: "pf-age", summary: "pf-summary",
};

let state = { avatar: "" };

export async function initProfile() {
  // 绑定添加按钮
  document.querySelectorAll("[data-add]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.add;
      state[name] = state[name] || [];
      state[name].push(emptyItem(name));
      renderRepeater(name);
    });
  });

  // 头像
  $("#pf-avatar-file").addEventListener("change", onAvatarPick);
  $("#btn-avatar-clear").addEventListener("click", () => { state.avatar = ""; paintAvatar(); });

  // 保存
  $("#btn-save-profile").addEventListener("click", saveProfile);

  // 导入导出
  $("#btn-export-profile").addEventListener("click", exportProfile);
  $("#pf-import-file").addEventListener("change", importProfile);

  await loadProfile();
}

async function loadProfile() {
  try {
    const p = await api.getProfile();
    state = { ...p, avatar: p.avatar || "" };
    for (const [key, id] of Object.entries(SCALARS)) {
      const node = document.getElementById(id);
      if (node) node.value = p[key] ?? "";
    }
    for (const name of Object.keys(REPEATERS)) {
      state[name] = Array.isArray(p[name]) ? p[name] : [];
      renderRepeater(name);
    }
    paintAvatar();
    setState(`已载入 · ${fmtTime(p.updated_at)}`);
  } catch (err) {
    toast(`载入资料失败：${err.message}`, "err");
  }
}

function emptyItem(name) {
  const item = {};
  for (const f of REPEATERS[name].fields) {
    item[f.key] = f.type === "lines" || f.type === "csv" ? [] : "";
  }
  return item;
}

// --------------------------- 渲染重复项 --------------------------- //
function renderRepeater(name) {
  const host = document.getElementById(`rep-${name}`);
  if (!host) return;
  host.innerHTML = "";
  const items = state[name] || [];
  if (!items.length) {
    host.appendChild(el("p", { class: "empty", text: "暂无，点击下方按钮添加。" }));
    return;
  }
  items.forEach((item, index) => host.appendChild(repItem(name, item, index)));
}

function repItem(name, item, index) {
  const def = REPEATERS[name];
  const grid = el("div", { class: "rep-grid" });
  for (const f of def.fields) {
    const value = toInputValue(item[f.key], f.type);
    const control = f.type === "textarea" || f.type === "lines"
      ? el("textarea", { rows: f.type === "lines" ? 2 : 3, placeholder: f.ph || "" })
      : el("input", { type: "text", placeholder: f.ph || "" });
    control.value = value;
    control.dataset.field = f.key;
    control.dataset.type = f.type || "text";
    const wrap = el("label", { class: `field${f.block ? " field-block" : ""}` }, [
      el("span", { text: f.label }), control,
    ]);
    grid.appendChild(wrap);
  }
  return el("div", { class: "rep-item", dataset: { name, index } }, [
    el("div", { class: "rep-head" }, [
      el("span", { class: "rep-index", text: `${String(index + 1).padStart(2, "0")} / ${def.title}` }),
      el("button", {
        class: "rep-del", type: "button", text: "移除",
        onclick: () => { state[name].splice(index, 1); renderRepeater(name); },
      }),
    ]),
    grid,
  ]);
}

function toInputValue(v, type) {
  if (v === null || v === undefined) return "";
  if (type === "lines") return Array.isArray(v) ? v.join("\n") : String(v);
  if (type === "csv") return Array.isArray(v) ? v.join(", ") : String(v);
  return String(v);
}

function fromInputValue(v, type) {
  const s = (v || "").trim();
  if (type === "lines") return s ? s.split("\n").map((x) => x.trim()).filter(Boolean) : [];
  if (type === "csv") return s ? s.split(/[,，]/).map((x) => x.trim()).filter(Boolean) : [];
  return s;
}

function collectRepeater(name) {
  const host = document.getElementById(`rep-${name}`);
  if (!host) return [];
  return Array.from(host.querySelectorAll(".rep-item")).map((node) => {
    const item = {};
    node.querySelectorAll("[data-field]").forEach((input) => {
      item[input.dataset.field] = fromInputValue(input.value, input.dataset.type);
    });
    return item;
  });
}

// --------------------------- 头像 --------------------------- //
function onAvatarPick(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) { toast("头像请小于 2MB", "warn"); return; }
  const reader = new FileReader();
  reader.onload = () => { state.avatar = String(reader.result); paintAvatar(); };
  reader.readAsDataURL(file);
}

function paintAvatar() {
  const node = $("#avatar-preview");
  if (!node) return;
  if (state.avatar) {
    node.style.backgroundImage = `url("${state.avatar}")`;
    node.textContent = "";
  } else {
    node.style.backgroundImage = "";
    node.textContent = "无头像";
  }
}

// --------------------------- 保存 / 导入导出 --------------------------- //
function collectProfile() {
  const out = {};
  for (const [key, id] of Object.entries(SCALARS)) {
    const node = document.getElementById(id);
    if (!node) continue;
    if (key === "age") out[key] = node.value === "" ? null : Number(node.value);
    else out[key] = node.value.trim();
  }
  out.avatar = state.avatar || "";
  for (const name of Object.keys(REPEATERS)) out[name] = collectRepeater(name);
  out.extra = state.extra || {};
  return out;
}

async function saveProfile() {
  const btn = $("#btn-save-profile");
  btn.disabled = true;
  try {
    const payload = collectProfile();
    const saved = await api.saveProfile(payload);
    state = { ...state, ...saved };
    setState(`已保存 · ${fmtTime(saved.updated_at)}`);
    toast("资料已保存", "ok");
  } catch (err) {
    toast(`保存失败：${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

function setState(text) {
  const node = $("#profile-state");
  if (node) node.textContent = text;
}

function exportProfile() {
  const data = JSON.stringify(collectProfile(), null, 2);
  const blob = new Blob([data], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: "profile.json" });
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  toast("已导出 profile.json", "ok");
}

function importProfile(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(String(reader.result));
      state = { ...state, ...data, avatar: data.avatar || "" };
      for (const [key, id] of Object.entries(SCALARS)) {
        const node = document.getElementById(id);
        if (node) node.value = data[key] ?? "";
      }
      for (const name of Object.keys(REPEATERS)) {
        state[name] = Array.isArray(data[name]) ? data[name] : [];
        renderRepeater(name);
      }
      paintAvatar();
      toast("已导入，记得点保存", "ok");
    } catch (err) {
      toast(`导入失败：${err.message}`, "err");
    }
  };
  reader.readAsText(file);
  ev.target.value = "";
}
