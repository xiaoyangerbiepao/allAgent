// frontend/src/api/llm.ts
// 大模型配置接口封装：厂商 / 模型目录、业务绑定、连通性测试、状态同步。
// 注意：后端不会返回任何 API Key，这里只拿 provider_id / model_id 这类标识。

import client from './client'

/** 模型档次：flash = 省钱快，pro = 擅长推理 */
export type ModelTier = 'flash' | 'pro'

/** 模型可用状态：not_activated 表示厂商侧「产品未开通」 */
export type ModelStatus = 'available' | 'not_activated' | 'error' | 'unknown'

export interface LlmModel {
  model_id: string
  display_name: string
  tier: ModelTier
  context_window: number | null
  supports_tools: boolean
  status: ModelStatus
  is_selectable: boolean
}

export interface LlmProvider {
  id: string
  display_name: string
  /** 后端是否配了该厂商的 Key（只给布尔值，不下发密钥） */
  has_key: boolean
  enabled: boolean
  models: LlmModel[]
}

export interface LlmBinding {
  provider_id: string
  model_id: string
  temperature: number
  /** 主模型不可用时是否自动切到备选 */
  failover_enabled: boolean
}

/** 熔断状态：closed 正常 / open 熔断中（会被跳过）/ half_open 冷却结束待探测 */
export type BreakerState = 'closed' | 'open' | 'half_open'

export interface ProviderHealth {
  state: BreakerState
  consecutive_failures: number
  total_failures: number
  cooldown_remaining: number
  last_error: string
  last_failure_at: number | null
  last_success_at: number | null
}

/** 切换链上的一个候选 */
export interface LlmChainEntry {
  provider_id: string
  provider_name: string
  model_id: string
  display_name: string
  tier: ModelTier | 'unknown'
  is_primary: boolean
  healthy: boolean
  breaker: BreakerState
}

export interface LlmAgent {
  agent_type: string
  label: string
  binding: LlmBinding | null
  /** 主 → 备 的切换顺序 */
  chain: LlmChainEntry[]
  /** 批改类为 false：评分口径固定，不跨厂商切换 */
  cross_provider_allowed: boolean
}

export interface LlmCatalog {
  agents: LlmAgent[]
  providers: LlmProvider[]
  loaded_from_db: boolean
  health: Record<string, ProviderHealth>
}

/** 单个模型的探测结果 */
export interface ProbeResult {
  ok: boolean
  latency_ms: number
  detail: string
  content?: string
  supports_tools?: boolean
}

export interface TestModelResponse {
  ok: boolean
  chat: ProbeResult
  tools?: ProbeResult
}

export interface SyncResultItem {
  provider_id: string
  model_id: string
  status: ModelStatus
  latency_ms: number | null
  supports_tools: boolean | null
  detail: string
}

/** 厂商 + 可选模型 + 各业务当前绑定，设置页只调这一个接口 */
export async function fetchCatalog(): Promise<LlmCatalog> {
  const { data } = await client.get<LlmCatalog>('/llm/catalog')
  return data
}

/** 修改某个业务的模型绑定，后端会立即清缓存，下一次调用即生效 */
export async function updateBinding(
  agentType: string,
  payload: {
    provider_id: string
    model_id: string
    temperature?: number
    failover_enabled?: boolean
  },
): Promise<{ ok: boolean; binding: LlmBinding; chain: LlmChainEntry[] }> {
  const { data } = await client.put(`/llm/bindings/${agentType}`, payload)
  return data
}

/** 手工解除熔断（改好密钥或确认厂商恢复后用） */
export async function resetHealth(providerId?: string): Promise<{
  ok: boolean
  health: Record<string, ProviderHealth>
}> {
  const { data } = await client.post('/llm/health/reset', null, {
    params: providerId ? { provider_id: providerId } : undefined,
  })
  return data
}

/** 测试某个「厂商 + 模型」能否调通，同时验证是否支持工具调用 */
export async function testModel(providerId: string, modelId: string): Promise<TestModelResponse> {
  // 探两次（chat + tools），单次约 1.5s，给足超时
  const { data } = await client.post<TestModelResponse>(
    '/llm/test',
    { provider_id: providerId, model_id: modelId, with_tools: true },
    { timeout: 60_000 },
  )
  return data
}

/** 逐个探测已上架的 6 个模型，回写可用状态；耗时 20-40 秒，单独放大超时 */
export async function syncModels(): Promise<{
  ok: boolean
  total: number
  available: number
  results: SyncResultItem[]
}> {
  const { data } = await client.post('/llm/sync', {}, { timeout: 180_000 })
  return data
}
