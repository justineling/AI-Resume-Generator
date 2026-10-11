// 后端 API 封装 + WebSocket 事件订阅

async function request(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.message)) || res.statusText;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status; // 供调用方区分 409 等语义（如级联删除确认）
    throw err;
  }
  return data;
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body),
  put: (url, body) => request("PUT", url, body),
  patch: (url, body) => request("PATCH", url, body),
  del: (url) => request("DELETE", url),

  // 资料
  getProfile: () => api.get("/api/profile"),
  saveProfile: (p) => api.put("/api/profile", p),
  // AI 面试问答（flashcard）
  profileInterview: (focus) => api.post("/api/profile/interview", { focus: focus || "" }),
  profileInterviewApply: (answers, autoGenerate = false) =>
    api.post("/api/profile/interview/apply", { answers, auto_generate: !!autoGenerate }),

  // 对话工作区（一个 chat = 一个岗位场景 = 一份简历）
  listChats: () => api.get("/api/chats"),
  createChat: (payload = {}) => api.post("/api/chats", payload),
  getChat: (id) => api.get(`/api/chats/${id}`),
  patchChat: (id, payload) => api.patch(`/api/chats/${id}`, payload),
  deleteChat: (id) => api.del(`/api/chats/${id}`),
  // per-chat 资料快照
  getChatProfile: (id) => api.get(`/api/chats/${id}/profile`),
  saveChatProfile: (id, p) => api.put(`/api/chats/${id}/profile`, p),
  resetChatProfile: (id) => api.post(`/api/chats/${id}/profile/reset`),
  // 消息
  listChatMessages: (id) => api.get(`/api/chats/${id}/messages`),
  postChatMessage: (id, payload) => api.post(`/api/chats/${id}/messages`, payload),
  // 生成 / 取消 / 重试 / 覆盖清单
  generateChat: (id) => api.post(`/api/chats/${id}/generate`),
  cancelChat: (id) => api.post(`/api/chats/${id}/cancel`),
  retryChat: (id) => api.post(`/api/chats/${id}/retry`),
  getCoverage: (id) => api.get(`/api/chats/${id}/coverage`),
  // 生成前的 AI 问答（岗位需要但资料缺依据的点）
  chatInterview: (id) => api.post(`/api/chats/${id}/interview`, {}),
  chatInterviewSubmit: (id, answers, autoGenerate = true) =>
    api.post(`/api/chats/${id}/interview/submit`, { answers, auto_generate: !!autoGenerate }),

  // 简历
  listResumes: () => api.get("/api/resumes"),
  getResume: (id) => api.get(`/api/resumes/${id}`),
  saveResume: (id, payload) => api.put(`/api/resumes/${id}`, payload),
  changeLayout: (id, payload) => api.post(`/api/resumes/${id}/layout`, payload),
  deleteResume: (id) => api.del(`/api/resumes/${id}`),
  downloadUrl: (id) => `/api/resumes/${id}/download`,
  previewUrl: (id) => `/api/resumes/${id}/preview`,

  // AI 编辑
  aiEdit: (id, payload) => api.post(`/api/resumes/${id}/ai-edit`, payload),
  listEdits: (id) => api.get(`/api/resumes/${id}/edits`),
  undoEdit: (id) => api.post(`/api/resumes/${id}/undo`),

  // 设置
  getLLMSettings: () => api.get("/api/settings/llm"),
  saveLLMSettings: (s) => api.put("/api/settings/llm", s),
  importFromUrl: (url, autoGenerate) =>
    api.post("/api/chats/import-url", { url, auto_generate: !!autoGenerate }),
  getProviders: () => api.get("/api/settings/providers"),
  testLLM: (payload) => api.post("/api/settings/llm/test", payload),
};

// --------------------------- WebSocket --------------------------- //
export function connectEvents(onEvent) {
  let ws = null;
  let retry = 0;
  let closed = false;

  const open = () => {
    if (closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/tasks`);
    ws.onopen = () => { retry = 0; };
    ws.onmessage = (ev) => {
      try { onEvent(JSON.parse(ev.data)); } catch { /* ignore */ }
    };
    ws.onclose = () => {
      if (closed) return;
      retry = Math.min(retry + 1, 6);
      setTimeout(open, 500 * retry); // 指数级退避重连
    };
    ws.onerror = () => ws && ws.close();
  };
  open();
  return () => { closed = true; ws && ws.close(); };
}
