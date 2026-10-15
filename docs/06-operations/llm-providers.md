# 运维：LLM 供应商配置

> 这篇讲系统内置了哪些模型供应商、怎么配置、点「测试连接」背后发生了什么，以及没有 Key 时的降级模式。
> 适合：要接入大模型的使用者；要改供应商预设或测试逻辑的开发者。

相关文件：`backend/app/providers.py`（预设注册表）、`backend/app/routers/settings.py`（设置与测试接口）、`backend/app/llm.py`（客户端与运行时配置）、前端设置弹窗在 `backend/app/static/js/app.js` + `index.html`。

---

## 1. 供应商预设机制

`providers.py` 是一张**纯数据**的预设表（无副作用、无网络请求），共 **10 个**预设。每个预设给出一个 OpenAI 兼容的 `base_url`，实际调用时 `/chat/completions` 拼在其后（如 `https://api.deepseek.com/chat/completions`）。前端通过 `GET /api/settings/providers` 拉取这张表来渲染下拉与模型候选。

| id | 名称 | Base URL | 预设模型（示例） | 需要 Key |
|---|---|---|---|---|
| **`deepseek`（默认）** | DeepSeek 深度求索 | `https://api.deepseek.com` | `deepseek-flash`、`deepseek-v4-pro` | 是 |
| `openai` | OpenAI | `https://api.openai.com/v1` | `gpt-4o`、`gpt-4o-mini` | 是 |
| `moonshot` | Moonshot Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k/32k/128k` | 是 |
| `dashscope` | 阿里云百炼（通义千问） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus`、`qwen-turbo`、`qwen-max` | 是 |
| `zhipu` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus`、`glm-4-flash` | 是 |
| `volcengine` | 火山方舟（豆包） | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-pro-32k`、`doubao-lite-32k` | 是 |
| `hunyuan` | 腾讯混元 | `https://api.hunyuan.cloud.tencent.com/v1` | `hunyuan-turbo`、`hunyuan-pro` | 是 |
| `siliconflow` | 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3`、`Qwen/Qwen2.5-7B-Instruct` | 是 |
| `ollama` | Ollama（本地部署） | `http://localhost:11434/v1` | `llama3.1`、`qwen2.5` | **否** |
| `custom` | 自定义 | （空，需手填） | （需手填） | 是 |

- **DeepSeek 排第一，也是默认供应商**（`DEFAULT_PROVIDER_ID = "deepseek"`）。全新安装、未保存过配置时会默认选中它。
- 非 DeepSeek 供应商的预设模型名可能随版本变化，预设里统一附了提示「建议点「测试连接」拉取真实列表」。
- `requires_key=False` 的 Ollama 通常无需 Key；`editable_base_url=True` 的只有 `custom`。

### 关于 DeepSeek（已核实）

| 项 | 值 |
|---|---|
| Base URL | `https://api.deepseek.com` |
| 模型 | `deepseek-flash`（性价比高）、`deepseek-v4-pro`（能力更强） |
| 申请 API Key | <https://platform.deepseek.com/api_keys> |
| 文档 | <https://api-docs.deepseek.com/> |

> 提示：`deepseek-flash` / `deepseek-v4-pro` 是按官方文档核实过的准确名称，请勿写成其它名字。

---

## 2. 配置步骤

1. 顶栏「设置」（或命令面板「设置（模型配置）」）打开弹窗 `#settings-dialog`。
2. **选供应商** `#cfg-provider`。
3. **Base URL 自动填充**：非 `custom` 供应商的 Base URL 输入框 `#cfg-base` 是 `readonly` 的，由预设自动填入；只有选「自定义 / Custom」时才可编辑。
4. **填 API Key** `#cfg-key`（密码框）。已配置过时占位提示「已配置时留空即可，无需重复输入」，上方 `#cfg-key-status` 会醒目显示「✓ API Key 已配置（sk-****a1b2）」或「尚未配置 API Key——当前使用本地降级模式」。
5. **点「测试连接」** `#btn-test`（可选，但强烈建议）→ 成功后会用返回的**真实模型列表**刷新 `#cfg-model-select`。
6. **选模型** `#cfg-model-select`；列表末尾有「＋ 自定义模型名…」，选中后出现输入框 `#cfg-model-custom`。
7. 可选：`#cfg-max-tokens`「最大输出 token」（0 = 不限制）、`#cfg-theme` 颜色模式（切换即时生效，不依赖保存）。
8. 点「保存设置」（`#btn-save-settings`）→ `PUT /api/settings/llm` → 关闭弹窗、刷新顶栏状态。

### 为什么不让你填 Base URL

供应商预设已经带上了正确的（OpenAI 兼容）Base URL，**用户无需也不应手填**——填错（少 `/v1`、多斜杠、用错域名）是接入失败最常见的原因。因此除「自定义」外 Base URL 一律只读。选「自定义」时才放开，让你接入任意 OpenAI 兼容端点（如企业内网网关）。

---

## 3. 「测试连接」怎么工作

`POST /api/settings/llm/test`（`routers/settings.py`），超时 15 秒：

1. 校验 `provider_id` 是否已知（未知返回 400）；解析 `base_url`（用传入值，否则用预设）。
2. 确定用于测试的 Key：
   - 传了 `api_key` 就用它；
   - **未填 Key 时回落到已存储的 Key，但仅当「当前测试的供应商」与「已配置的供应商」一致时**（自定义供应商还要 Base URL 相同）。目的是避免拿 A 家的 Key 去测 B 家造成误判。
   - 仍然没有 Key → 直接返回「请先填写 API Key」。
3. **先打 `GET {base_url}/models`**（带 `Authorization: Bearer <key>`）：
   - `200` → 成功；解析返回的模型列表（兼容 `data` / `models` / `result` / 数组等多种结构）。若列表为空则回落到预设模型名。返回中文结论如「连接成功 · 延迟 123ms · 发现 18 个可用模型」。
   - `404` / `405` → 说明该端点没实现 `/models`，**回退发一次最小的 `/chat/completions` 探测**（一条 `hi`、`max_tokens=1`）来判断 Key 是否有效：200 → 成功；否则按失败码给结论。
   - 其它状态码 → 按码给可读结论（见下）。
4. 网络异常（超时 / 连不上 / 其它 HTTP 错误）→ 返回对应的中文提示。

失败结论映射：

| 状态码 | 提示 |
|---|---|
| 401 / 403 | API Key 无效或已过期 |
| 404 | 接口地址不正确，请检查 Base URL |
| 405 | 接口不支持该请求方式，请检查 Base URL |
| 429 | 请求过于频繁或额度不足 |
| ≥ 500 | 服务端错误（HTTP xxx），请稍后重试 |
| 其它 | 连接失败（HTTP xxx） |

> 测试接口**绝不把 API Key 写进日志或错误信息**。

---

## 4. `max_tokens` 与温度

| 参数 | 语义 |
|---|---|
| `max_tokens` | **`<= 0` 表示「不限制」**：构造请求时**完全不发送** `max_tokens` 字段，由模型服务端用自己的默认/最大上限决定。设置里填 `0` 就是「不限制」（默认值）；**只有负数会被拒绝**（400「最大输出 token 不能为负数」） |
| 温度 | **不发送**该字段：由模型服务端使用自己的默认值（`temperature` 仍在配置里保存/返回，但请求体里不带它） |

若模型输出被截断（`finish_reason == "length"`），系统会抛出可读错误：设置了上限时提示「模型输出被 max_tokens 截断（当前上限 N）。请在设置中调高后重试，或缩短岗位描述」；不限制模式下则提示「被服务端上限截断，请缩短岗位描述或换支持更大输出的模型」。

---

## 5. 降级模式（无 API Key）

没有配置 Key 时，系统自动走 `backend/app/mock.py` 的启发式实现，**功能链路完整、不调用大模型**：

- 顶栏状态显示「LLM // 降级模式 · worker N」，页脚显示「LLM: MOCK (启发式)」。
- 岗位解析、内容生成、覆盖清单、AI 问答、AI 修改都有确定性实现（质量不如 LLM，但流程一致）。
- 界面在设置弹窗里也会提示「留空 API Key 则使用本地降级模式（启发式规则），功能完整但不调用大模型」。

---

## 6. 运行时配置的读取优先级

`llm.get_runtime_config()` 决定实际用哪套配置：

1. **DB 里存过配置**（保存设置会写 `AppSetting`，键含 `llm_provider_id` / `llm_base_url` / `llm_model` 等）→ 以 DB 为准；`provider_id` 缺失时按存储的 `base_url` 反查，反查不到归为 `custom`。
2. **只通过 `.env` 配了 Key** → 尊重 `.env` 的 `base_url` / `model`。
3. **全新安装且未配 Key** → 默认用 DeepSeek 预设（开箱即用）。

> 也就是说：**界面上「保存设置」写入的配置会覆盖 `.env`**；`.env` 只作为未配置时的初值。

---

## 常见陷阱

1. **`PUT /api/settings/llm` 不带 `provider_id` 时按 `base_url` 反查**：只传 `base_url`（或只改 `worker_count` / `temperature`）时，后端会：给了 `base_url` → 按它反查供应商、匹配不到归为 `custom`；两者都没给 → **不动**已存的 `provider_id`。因此别只改 `base_url` 却期望供应商不变——它会跟着重算，避免「新的 base_url + 旧的 provider_id」自相矛盾。
2. **已配置时留空 API Key = 保持不变**（不会清空）。想换 Key 就直接输入新 Key。
3. **「测试连接」用已存储 Key 只在同供应商时生效**：切到别的供应商后不填 Key 直接测试，会提示「请先填写 API Key」，这是防止误判的设计。
4. **`custom` 必须显式提供 Base URL**：预设里 `custom` 的 `base_url` 为空，不填就测试/保存会报「请填写 Base URL」。
5. **预设模型名可能过时**：非 DeepSeek 的候选名只是示例，点「测试连接」拉真实列表再选，或用「自定义模型名」。
6. **`.env.example` 里的 DeepSeek 示例已过时**：它写的是 `https://api.deepseek.com/v1` + `deepseek-chat`，而当前预设是 `https://api.deepseek.com` + `deepseek-flash` / `deepseek-v4-pro`。以**设置界面/`providers.py` 的预设为准**，或直接用「测试连接」探测。
