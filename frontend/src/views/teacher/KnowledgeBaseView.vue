<template>
  <div class="kb-manage">
    <!-- 上传区 -->
    <el-card>
      <template #header><span>📚 知识库管理 · 上传文档</span></template>

      <div class="file-picker">
        <el-button type="default" :icon="UploadFilled" @click="triggerFilePicker">
          选择文档（PDF / Markdown）
        </el-button>
        <input
          ref="fileInputRef"
          type="file"
          accept=".pdf,.md,.markdown"
          style="display: none"
          @change="handleFileChange"
        />
        <span v-if="selectedFile" class="file-name">{{ selectedFile.name }}</span>
        <el-button v-if="selectedFile" text type="danger" size="small" @click="clearFile">
          移除
        </el-button>
      </div>

      <div class="form-row">
        <span class="form-label">课程 ID（可选）</span>
        <el-input
          v-model="courseId"
          placeholder="填了之后，智能问答可按课程过滤检索范围"
          style="max-width: 420px"
          clearable
        />
      </div>

      <div class="form-row">
        <el-switch v-model="useContext" />
        <span class="form-label">
          启用 Contextual RAG
          <span class="sub-tip">为每个分块用 LLM 生成定位描述，检索更准，但更慢且消耗 API 配额</span>
        </span>
      </div>

      <el-button
        type="primary"
        :loading="uploading"
        :disabled="!selectedFile"
        style="margin-top: 16px"
        @click="handleUpload"
      >
        上传并向量化入库
      </el-button>
      <div class="upload-tip">
        流程：解析分块 → 向量化（BGE-M3 稠密+稀疏）→ 写入 Milvus。上传后后台处理，页面下方轮询进度。
      </div>
    </el-card>

    <!-- 文档列表 -->
    <el-card style="margin-top: 16px">
      <template #header>
        <span>已上传文档</span>
        <el-button style="float: right" :icon="Refresh" circle size="small" @click="fetchList" />
      </template>

      <el-empty v-if="!documents.length" description="还没有上传过文档" />
      <el-table v-else :data="documents" size="small">
        <el-table-column prop="filename" label="文件名" min-width="200" show-overflow-tooltip />
        <el-table-column label="大小" width="90">
          <template #default="{ row }">{{ formatSize(row.size_bytes) }}</template>
        </el-table-column>
        <el-table-column label="分块数" width="80">
          <template #default="{ row }">
            <span v-if="row.status === 'completed'">{{ row.chunk_count }}</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="上下文增强" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.use_context" type="warning" size="small">已启用</el-tag>
            <el-tag v-else type="info" size="small">未启用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.status === 'processing'" type="primary" size="small">
              <el-icon class="is-loading" style="vertical-align: -2px"><Loading /></el-icon>
              入库中
            </el-tag>
            <el-tooltip v-else-if="row.status === 'failed'" :content="row.error_msg || '处理失败'" placement="top">
              <el-tag type="danger" size="small">失败</el-tag>
            </el-tooltip>
            <el-tag v-else type="success" size="small">已完成</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="上传时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button text type="danger" size="small" @click="handleDelete(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Refresh, Loading } from '@element-plus/icons-vue'
import { kbApi, type KbDocument } from '@/api/kb'

const ACCEPT_EXTENSIONS = ['.pdf', '.md', '.markdown']
const POLL_INTERVAL_MS = 5000

const selectedFile = ref<File | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const courseId = ref('')
const useContext = ref(false)
const uploading = ref(false)
const documents = ref<KbDocument[]>([])
let pollTimer: number | undefined

function triggerFilePicker() {
  fileInputRef.value?.click()
}

function handleFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  if (!file) return

  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!ACCEPT_EXTENSIONS.includes(ext)) {
    ElMessage.error('仅支持 .pdf / .md / .markdown 文件')
    input.value = ''
    return
  }
  if (file.size > 50 * 1024 * 1024) {
    ElMessage.error('文件超过 50MB 限制')
    input.value = ''
    return
  }
  selectedFile.value = file
}

function clearFile() {
  selectedFile.value = null
  if (fileInputRef.value) fileInputRef.value.value = ''
}

async function handleUpload() {
  if (!selectedFile.value) return
  uploading.value = true
  try {
    const { data } = await kbApi.upload(selectedFile.value, courseId.value.trim(), useContext.value)
    ElMessage.success(`已提交入库任务：${data.filename}`)
    clearFile()
    await fetchList()
    startPolling()
  } catch {
    // 错误提示由 axios 拦截器统一处理
  } finally {
    uploading.value = false
  }
}

async function fetchList() {
  try {
    const { data } = await kbApi.list()
    documents.value = data.documents
  } catch {
    // 拦截器统一提示
  }
}

async function handleDelete(row: KbDocument) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.filename}」吗？将同时清理 Milvus 中该文档的全部分块。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    const { data } = await kbApi.remove(row.id)
    if (data.warning) ElMessage.warning(data.warning)
    else ElMessage.success('已删除')
    await fetchList()
  } catch {
    // 拦截器统一提示
  }
}

// 有入库中的任务时每 5 秒轮询一次，全部完成/失败后自动停止
function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    await fetchList()
    if (!documents.value.some(d => d.status === 'processing')) stopPolling()
  }, POLL_INTERVAL_MS)
}

function stopPolling() {
  if (pollTimer !== undefined) {
    clearInterval(pollTimer)
    pollTimer = undefined
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 19)
}

onMounted(async () => {
  await fetchList()
  if (documents.value.some(d => d.status === 'processing')) startPolling()
})

onUnmounted(stopPolling)
</script>

<style scoped>
.kb-manage {
  max-width: 1080px;
}

.file-picker {
  display: flex;
  align-items: center;
  gap: 12px;
}

.file-name {
  color: #409eff;
}

.form-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}

.form-label {
  color: #606266;
  font-size: 14px;
}

.sub-tip {
  color: #909399;
  font-size: 12px;
  margin-left: 8px;
}

.upload-tip {
  margin-top: 10px;
  color: #909399;
  font-size: 12px;
}
</style>
