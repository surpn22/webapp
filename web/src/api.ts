export type Mode = 'text' | 'markdown'
export type JobStatus = 'queued' | 'running' | 'done' | 'cancelled' | 'interrupted' | 'failed'

export type Kind = 'chat' | 'folder'

/** A chat (holds files and OCR results) or a folder that groups chats. */
export interface Folder {
  id: number
  name: string
  kind: Kind
  parent_id: number | null
  created_at: number
  updated_at: number
  documents: number
  active: number
}

export interface Line { text: string; confidence: number; row: number }
export interface Dropped { text: string; confidence: number; reason: string }

export interface Page {
  id: number
  job_id: number
  document_id: number
  page_no: number
  mode: Mode
  status: 'ok' | 'incomplete' | 'failed'
  content: string
  meta: { lines: Line[]; dropped: Dropped[] } | null
  error: string | null
  seconds: number | null
}

export interface Doc { id: number; filename: string; page_count: number; size: number }

export interface Job {
  id: number
  folder_id: number
  document_id: number
  mode: Mode
  status: JobStatus
  page_total: number
  pages_done: number
  pages_failed: number
  error: string | null
  created_at: number
  started_at: number | null
  finished_at: number | null
  document?: Doc
  pages?: Page[]
}

export interface UploadPayload {
  mode: Mode
  files: { document_id: number; filename: string; pages: number }[]
  rejected: { filename: string; reason: string }[]
}

export interface Message {
  id: number
  folder_id: number
  role: 'user' | 'assistant'
  type: 'upload' | 'ocr_result' | string
  payload: any
  created_at: number
  job?: Job
}

export interface SearchHit {
  page_id: number
  job_id: number
  page_no: number
  mode: Mode
  filename: string
  document_id: number
  chat_id: number
  snippet: { before: string; match: string; after: string }
}

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method }
  if (body instanceof FormData) init.body = body
  else if (body !== undefined) {
    init.body = JSON.stringify(body)
    init.headers = { 'Content-Type': 'application/json' }
  }
  const res = await fetch(url, init)
  if (!res.ok) {
    let msg = `เกิดข้อผิดพลาด (${res.status})`
    try {
      const j = await res.json()
      if (typeof j.detail === 'string') msg = j.detail
    } catch { /* not JSON */ }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

export const api = {
  folders: () => call<Folder[]>('GET', '/api/folders'),
  create: (kind: Kind, name = '', parentId: number | null = null) =>
    call<Folder>('POST', '/api/folders', { kind, name, parent_id: parentId }),
  rename: (id: number, name: string) => call<Folder>('PATCH', `/api/folders/${id}`, { name }),
  move: (id: number, parentId: number | null) => call<Folder>('PATCH', `/api/folders/${id}`, { parent_id: parentId }),
  deleteFolder: (id: number) => call<{ ok: boolean }>('DELETE', `/api/folders/${id}`),
  messages: (folderId: number) => call<Message[]>('GET', `/api/folders/${folderId}/messages`),
  upload: (folderId: number, files: File[], mode: Mode) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    fd.append('mode', mode)
    return call<{ jobs: number[]; rejected: { filename: string; reason: string }[] }>(
      'POST', `/api/folders/${folderId}/upload`, fd)
  },
  job: (id: number) => call<Job>('GET', `/api/jobs/${id}`),
  cancel: (id: number) => call('POST', `/api/jobs/${id}/cancel`),
  resume: (id: number) => call('POST', `/api/jobs/${id}/resume`),
  retryFailed: (id: number) => call('POST', `/api/jobs/${id}/retry-failed`),
  rerun: (docId: number, mode: Mode) => call<{ job_id: number }>('POST', `/api/documents/${docId}/rerun`, { mode }),
  search: (folderId: number, q: string) =>
    call<SearchHit[]>('GET', `/api/folders/${folderId}/search?q=${encodeURIComponent(q)}`),
  status: () => call<{ markdown_loaded: boolean; busy: Record<Mode, number | null> }>('GET', '/api/status'),
  unloadMarkdown: () => call<{ freed: boolean }>('POST', '/api/models/markdown/unload'),
}

export const downloadUrl = (jobId: number, fmt: 'md' | 'txt') => `/api/jobs/${jobId}/download?fmt=${fmt}`
export const pageImageUrl = (docId: number, n: number) => `/api/documents/${docId}/pages/${n}/image`

/** Exactly what the download holds: every page's raw output, joined as the scripts join them. */
export const jobText = (job: Job) => (job.pages ?? []).map((p) => p.content).join('\n\n')

export const MODE_LABEL: Record<Mode, string> = { text: 'OCR Text', markdown: 'OCR Markdown' }

export const MODE_HELP: Record<Mode, string> = {
  text: 'ข้อความไทย/อังกฤษล้วน เร็วมาก (ไม่ถึง 1 วินาที/หน้า) ไม่แยกตาราง อ่านลายมือได้ไม่ดี',
  markdown: 'ได้หัวข้อและตารางแบบ Markdown แม่นกว่า อ่านลายมือได้ดีกว่า แต่ช้า (ราว 25–35 วินาที/หน้า)',
}
