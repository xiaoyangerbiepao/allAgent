import client from './client'

export interface KbDocument {
  id: string
  filename: string
  course_id: string | null
  size_bytes: number
  chunk_count: number
  use_context: boolean
  status: 'processing' | 'completed' | 'failed'
  error_msg: string | null
  uploaded_by: string | null
  created_at: string | null
  finished_at: string | null
}

export interface KbDocumentListResponse {
  total: number
  documents: KbDocument[]
}

export interface KbUploadResponse {
  document_id: string
  filename: string
  status: string
  message: string
}

export const kbApi = {
  /** 上传文档并启动后台向量化入库（立即返回，处理进度靠列表轮询） */
  upload: (file: File, courseId: string, useContext: boolean) => {
    const form = new FormData()
    form.append('file', file)
    form.append('course_id', courseId)
    form.append('use_context', String(useContext))
    return client.post<KbUploadResponse>('/kb/documents', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  list: () => client.get<KbDocumentListResponse>('/kb/documents'),

  remove: (docId: string) => client.delete(`/kb/documents/${docId}`),
}
