// 通用 UI 工具：DOM 助手、toast、表单读写、防抖

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// --------------------------- Toast --------------------------- //
export function toast(message, kind = "info", timeout = 3600) {
  const box = document.getElementById("toasts");
  const node = el("div", { class: `toast is-${kind}`, text: message });
  box.appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity 200ms";
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 220);
  }, timeout);
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function statusLabel(status) {
  return {
    queued: "排队中", running: "生成中", succeeded: "已完成",
    failed: "失败", cancelled: "已取消",
  }[status] || status;
}

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// --------------------------- 确认弹窗 --------------------------- //
// 替代浏览器原生 window.confirm：项目 Ark 风格的 <dialog>，返回 Promise<boolean>。
// index.html 需存在 #confirm-dialog（#confirm-title / #confirm-message / #btn-confirm-ok / #btn-confirm-cancel）。
//
// 只允许同时存在一个确认请求：dialog 元素本身是单例，showModal 重复调用会抛
// InvalidStateError，因此用一个模块级 Promise 串行化；若上一个还没关，直接返回 false
// （调用方都是用户点击触发，正常操作不可能并发到这一步）。
let _confirmBusy = null;

export function confirmDialog({ title = "请确认", message = "", confirmText = "确定", danger = true } = {}) {
  if (_confirmBusy) return Promise.resolve(false);
  const dlg = document.getElementById("confirm-dialog");
  if (!dlg) return Promise.resolve(window.confirm(message)); // 兜底：结构缺失时退回原生
  _confirmBusy = new Promise((resolve) => {
    const ok = document.getElementById("btn-confirm-ok");
    const cancel = document.getElementById("btn-confirm-cancel");
    $("#confirm-title", dlg).textContent = title;
    $("#confirm-message", dlg).textContent = message;
    ok.textContent = confirmText;
    ok.className = danger ? "btn btn-danger" : "btn btn-primary";

    const done = (result) => {
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onCancel);
      dlg.removeEventListener("close", onClose);
      if (dlg.open) dlg.close();
      _confirmBusy = null;
      resolve(result);
    };
    const onOk = () => done(true);
    const onCancel = () => done(false);
    // close 事件覆盖 Esc / click backdrop 之外的所有关闭路径（cancel 之后还会发 close，
    // done 里已把监听摘掉，不会二次 resolve）
    const onClose = () => done(false);

    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onCancel);
    dlg.addEventListener("close", onClose);
    dlg.showModal();
    ok.focus();   // 默认焦点在主操作上，Enter 即确认
  });
  return _confirmBusy;
}

