<template>
  <div class="model-settings">
    <el-card v-loading="loading">
      <template #header>
        <span>模型配置</span>
        <div class="header-actions">
          <el-tag v-if="catalog" size="small" :type="catalog.loaded_from_db ? 'success' : 'warning'">
            {{ catalog.loaded_from_db ? '配置已从数据库加载' : '数据库未就绪，当前为默认值' }}
          </el-tag>
          <el-button size="small" :icon="Refresh" :loading="syncing" @click="handleSync">
            同步校验
          </el-button>
        </div>
      </template>

      <el-alert type="info" :closable="false" show-icon class="tip">
        每个业务可以单独选模型：<b>flash</b> 省钱快、<b>pro</b> 擅长推理。保存后立即生效，无需重启服务。
        密钥只存在于后端 <code>.env.local</code>，不会下发到浏览器。
      </el-alert>

      <div class="layout">
        <!-- 左栏：业务 Agent 列表 -->
        <div class="agent-list">
          <div
            v-for="agent in catalog?.agents ?? []"
            :key="agent.agent_type"
            class="agent-item"
            :class="{ 'agent-item--active': agent.agent_type === current?.agent_type }"
            @click="selectAgent(agent)"
          >
            <div class="agent-label">{{ agent.label }}</div>
            <div class="agent-model">{{ modelText(agent.binding) }}</div>
          </div>
        </div>

        <!-- 右栏：该业务的模型配置 -->
        <div v-if="current" class="config-panel">
          <div class="panel-title">{{ current.label }}</div>

          <el-form label-width="76px">
            <el-form-item label="厂商">
              <el-select v-model="form.providerId" style="width: 260px" @change="handleProviderChange">
                <el-option
                  v-for="p in catalog?.providers ?? []"
                  :key="p.id"
                  :value="p.id"
                  :label="p.display_name"
                  :disabled="!p.enabled"
                >
                  <span>{{ p.display_name }}</span>
                  <span v-if="!p.has_key" class="opt-hint">未配置密钥</span>
                </el-option>
              </el-select>
            </el-form-item>

            <el-form-item label="模型">
              <el-radio-group v-model="form.modelId" class="model-cards">
                <el-radio
                  v-for="m in currentModels"
                  :key="m.model_id"
                  :value="m.model_id"
                  :disabled="!canSelect(m)"
                >
                  <div class="model-card">
                    <div class="model-head">
                      <span class="model-name">{{ m.display_name }}</span>
                      <el-tag :type="m.tier === 'flash' ? 'success' : 'warning'" size="small">
                        {{ m.tier === 'flash' ? '省钱' : '推理' }}
                      </el-tag>
                    </div>
                    <div class="model-meta">
                      <el-tag v-if="m.status !== 'available'" type="danger" size="small">
                        {{ statusText(m.status) }}
                      </el-tag>
                      <span v-if="!m.supports_tools" class="meta-warn">不支持工具调用</span>
                      <span v-if="m.context_window" class="meta">
                        {{ Math.round(m.context_window / 1024) }}K 上下文
                      </span>
                    </div>
                  </div>
                </el-radio>
              </el-radio-group>
            </el-form-item>

            <el-form-item label="温度">
              <el-slider
                v-model="form.temperature"
                :min="0"
                :max="1"
                :step="0.1"
                show-input
                style="max-width: 420px"
              />
            </el-form-item>

            <el-form-item>
              <el-button :loading="testing" @click="handleTest">测试连接</el-button>
              <el-button type="primary" :loading="saving" :disabled="!dirty" @click="handleSave">
                保存
              </el-button>
              <el-button text :disabled="!dirty" @click="resetForm">重置</el-button>
              <span v-if="dirty" class="dirty-hint">有未保存的修改</span>
            </el-form-item>
          </el-form>

          <el-alert
            v-if="testResult"
            :type="testResult.ok ? 'success' : 'error'"
            :closable="true"
            show-icon
            class="test-result"
            @close="testResult = null"
          >
            <div v-if="testResult.ok">
              对话调用成功（{{ testResult.chat.latency_ms }} ms，返回「{{ testResult.chat.content }}」）
              <span v-if="testResult.tools">
                ；工具调用{{ testResult.tools.supports_tools ? '正常' : '未触发（可能影响结构化输出）' }}
                （{{ testResult.tools.latency_ms }} ms）
              </span>
            </div>
            <div v-else>{{ testResult.chat.detail }}</div>
          </el-alert>

          <div class="footnote">
            提示：「同步校验」会真的向 6 个模型各发一次最小请求，约需 20-40 秒，用于确认它们当前是否可调通。
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElNotification } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  fetchCatalog,
  syncModels,
  testModel,
  updateBinding,
  type LlmAgent,
  type LlmBinding,
  type LlmCatalog,
  type LlmModel,
  type TestModelResponse,
} from '@/api/llm'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const syncing = ref(false)

const catalog = ref<LlmCatalog | null>(null)
const current = ref<LlmAgent | null>(null)
const testResult = ref<TestModelResponse | null>(null)

const form = reactive({
  providerId: '',
  modelId: '',
  temperature: 0,
})

/** 当前厂商下可选的模型 */
const currentModels = computed<LlmModel[]>(() => {
  const provider = catalog.value?.providers.find(p => p.id === form.providerId)
  return provider?.models ?? []
})

/** 与后端保存值是否有差异（用于置灰保存按钮） */
const dirty = computed(() => {
  const b = current.value?.binding
  if (!b) return true
  return (
    b.provider_id !== form.providerId ||
    b.model_id !== form.modelId ||
    Math.abs(b.temperature - form.temperature) > 1e-6
  )
})

function modelText(binding: LlmBinding | null): string {
  if (!binding) return '未配置'
  const provider = catalog.value?.providers.find(p => p.id === binding.provider_id)
  const model = provider?.models.find(m => m.model_id === binding.model_id)
  return model?.display_name ?? binding.model_id
}

/** 表单里当前选中模型的展示名（保存成功提示用） */
const selectedModelLabel = computed(() => {
  const provider = catalog.value?.providers.find(p => p.id === form.providerId)
  const model = provider?.models.find(m => m.model_id === form.modelId)
  return model?.display_name ?? form.modelId
})

function statusText(status: string): string {
  if (status === 'not_activated') return '未开通'
  if (status === 'error') return '不可用'
  if (status === 'unknown') return '未校验'
  return status
}

function canSelect(m: LlmModel): boolean {
  return m.is_selectable && (m.status === 'available' || m.status === 'unknown')
}

/** 切换到某个厂商时，自动选中该厂商下与原来档次相同的可用模型 */
function pickDefaultModel(keepTier = false) {
  const models = currentModels.value.filter(canSelect)
  if (!models.length) {
    form.modelId = ''
    return
  }
  if (keepTier) {
    const previousTier =
      catalog.value?.providers
        .flatMap(p => p.models)
        .find(m => m.model_id === form.modelId)?.tier
    const sameTier = models.find(m => m.tier === previousTier)
    form.modelId = (sameTier ?? models[0]).model_id
    return
  }
  form.modelId = models[0].model_id
}

function resetForm() {
  const binding = current.value?.binding
  form.providerId = binding?.provider_id ?? catalog.value?.providers.find(p => p.enabled)?.id ?? ''
  form.modelId = binding?.model_id ?? ''
  form.temperature = binding?.temperature ?? 0
  if (!form.modelId) pickDefaultModel()
  testResult.value = null
}

function selectAgent(agent: LlmAgent) {
  current.value = agent
  resetForm()
}

function handleProviderChange() {
  pickDefaultModel(true)
  testResult.value = null
}

async function loadCatalog(keepSelection = false) {
  loading.value = true
  try {
    catalog.value = await fetchCatalog()
    const agentType = keepSelection ? current.value?.agent_type : undefined
    const agents = catalog.value.agents
    current.value = agents.find(a => a.agent_type === agentType) ?? agents[0] ?? null
    resetForm()
  } catch {
    // 错误提示由 axios 拦截器统一处理
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  if (!current.value) return
  saving.value = true
  try {
    await updateBinding(current.value.agent_type, {
      provider_id: form.providerId,
      model_id: form.modelId,
      temperature: form.temperature,
    })
    ElMessage.success(`已切换：${current.value.label} → ${selectedModelLabel.value}`)
    await loadCatalog(true)
  } catch {
    // 拦截器已提示
  } finally {
    saving.value = false
  }
}

async function handleTest() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await testModel(form.providerId, form.modelId)
  } catch {
    // 拦截器已提示
  } finally {
    testing.value = false
  }
}

async function handleSync() {
  syncing.value = true
  try {
    const result = await syncModels()
    await loadCatalog(true)
    ElNotification({
      title: '同步完成',
      message: `${result.total} 个模型中 ${result.available} 个可调用`,
      type: result.available === result.total ? 'success' : 'warning',
    })
  } catch {
    // 拦截器已提示
  } finally {
    syncing.value = false
  }
}

onMounted(() => loadCatalog())
</script>

<style scoped>
.header-actions {
  float: right;
  display: flex;
  align-items: center;
  gap: 8px;
}

.tip {
  margin-bottom: 16px;
}

.layout {
  display: flex;
  gap: 20px;
  align-items: flex-start;
}

.agent-list {
  width: 220px;
  flex-shrink: 0;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
}

.agent-item {
  padding: 12px 14px;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
  transition: background-color 0.2s;
}

.agent-item:last-child {
  border-bottom: none;
}

.agent-item:hover {
  background-color: #f7f9fc;
}

.agent-item--active {
  background-color: #f0f7ff;
  box-shadow: inset 3px 0 0 #1677ff;
}

.agent-label {
  font-size: 14px;
  color: #1f2329;
}

.agent-model {
  margin-top: 4px;
  font-size: 12px;
  color: #8a919f;
}

.config-panel {
  flex: 1;
  min-width: 0;
}

.panel-title {
  font-size: 15px;
  font-weight: 500;
  margin-bottom: 16px;
  color: #1f2329;
}

.model-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.model-cards :deep(.el-radio) {
  margin-right: 0;
  height: auto;
  padding: 10px 14px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  align-items: flex-start;
}

.model-cards :deep(.el-radio.is-checked) {
  border-color: #1677ff;
  background-color: #f0f7ff;
}

.model-cards :deep(.el-radio__label) {
  white-space: normal;
  padding-left: 8px;
}

.model-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.model-name {
  font-size: 13px;
  color: #1f2329;
}

.model-meta {
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #8a919f;
}

.meta-warn {
  color: #d4380d;
}

.opt-hint {
  float: right;
  font-size: 12px;
  color: #a8abb2;
}

.dirty-hint {
  margin-left: 12px;
  font-size: 12px;
  color: #d48806;
}

.test-result {
  margin-top: 8px;
}

.footnote {
  margin-top: 12px;
  font-size: 12px;
  color: #8a919f;
}
</style>
