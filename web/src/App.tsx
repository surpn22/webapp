import { useCallback, useEffect, useRef, useState } from 'react'
import { api, MODE_LABEL, type Folder, type Job, type Message, type Mode, type Page, type SearchHit, type UploadPayload } from './api'
import { Composer, type ComposerHandle } from './Composer'
import { ResultCard } from './ResultCard'
import { Viewer } from './Viewer'

/** What the main pane shows: a fresh chat (optionally inside a folder), a chat, or a folder's page. */
type View = { type: 'new'; parentId: number | null } | { type: 'chat'; id: number } | { type: 'folder'; id: number }

const LAST = 'thai-ocr:view'
const remember = (v: View) => { try { localStorage.setItem(LAST, JSON.stringify(v)) } catch { /* no storage */ } }
const recall = (): View => {
  try {
    const v = JSON.parse(localStorage.getItem(LAST) ?? '')
    if (v && (v.type === 'chat' || v.type === 'folder') && typeof v.id === 'number') return v
  } catch { /* nothing saved */ }
  return { type: 'new', parentId: null }
}
const time = (t: number) => new Date(t * 1000).toLocaleString('th-TH', { dateStyle: 'medium', timeStyle: 'short' })
const chatName = (files: File[]) =>
  files[0].name.replace(/\.[^.]+$/, '') + (files.length > 1 ? ` และอีก ${files.length - 1} ไฟล์` : '')

export default function App() {
  const [items, setItems] = useState<Folder[]>([])
  const [view, setView] = useState<View>(recall)
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [currentPage, setCurrentPage] = useState<Record<number, number>>({})
  const [cancelling, setCancelling] = useState<Set<number>>(new Set())
  const [modelLoading, setModelLoading] = useState(false)
  const [compare, setCompare] = useState<{ jobId: number; page: number } | null>(null)
  const [naming, setNaming] = useState('')            // name for a new folder, while its dialog is open
  const [creatingFolder, setCreatingFolder] = useState(false)
  const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(null)
  const [moving, setMoving] = useState<Folder | null>(null)
  const [deleting, setDeleting] = useState<Folder | null>(null)
  const [open, setOpen] = useState<Set<number>>(new Set())
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [dragging, setDragging] = useState(false)
  const chatId = view.type === 'chat' ? view.id : null
  const chatRef = useRef(chatId)
  const composer = useRef<ComposerHandle>(null)
  const bottom = useRef<HTMLDivElement>(null)
  chatRef.current = chatId

  const fail = useCallback((msg: string) => { setError(msg); setTimeout(() => setError(''), 6000) }, [])

  const loadItems = useCallback(() => api.folders().then((fs) => {
    setItems(fs)
    // the shown chat or folder was deleted elsewhere: start a new chat instead
    setView((v) => (v.type !== 'new' && !fs.some((f) => f.id === v.id) ? { type: 'new', parentId: null } : v))
  }).catch((e) => fail(e.message)), [fail])

  const loadMessages = useCallback((id: number, scroll = false) => api.messages(id).then((ms) => {
    if (chatRef.current !== id) return
    setMessages(ms)
    if (scroll) requestAnimationFrame(() => bottom.current?.scrollIntoView({ block: 'end' }))
  }).catch((e) => fail(e.message)), [fail])

  useEffect(() => { loadItems() }, [loadItems])
  useEffect(() => {
    remember(view)
    setHits(null)
    setQuery('')
  }, [view])
  useEffect(() => {
    if (chatId) {
      setLoading(true)
      loadMessages(chatId, true).finally(() => setLoading(false))
    } else setMessages([])
  }, [chatId, loadMessages])

  // keep the folder of the open chat expanded
  const current = view.type === 'new' ? undefined : items.find((f) => f.id === view.id)
  useEffect(() => {
    const parent = current?.kind === 'chat' ? current.parent_id : null
    if (parent) setOpen((s) => (s.has(parent) ? s : new Set(s).add(parent)))
  }, [current?.id, current?.parent_id, current?.kind])

  // Live progress from the server
  useEffect(() => {
    const es = new EventSource('/api/events')
    const patchJob = (job: Job, page?: Page) => setMessages((ms) => ms.map((m) => {
      if (m.type !== 'ocr_result' || m.job?.id !== job.id) return m
      const pages = page && !m.job.pages?.some((p) => p.id === page.id)
        ? [...(m.job.pages ?? []), page].sort((a, b) => a.page_no - b.page_no)
        : m.job.pages
      return { ...m, job: { ...m.job, ...job, document: m.job.document, pages } }
    }))
    const patchJobFull = (full: Job) => setMessages((ms) => ms.map((m) =>
      m.type === 'ocr_result' && m.job?.id === full.id ? { ...m, job: full } : m))
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data)
      if (ev.kind === 'folders') {
        loadItems()
      } else if (ev.kind === 'messages') {
        if (ev.folder_id === chatRef.current) loadMessages(ev.folder_id, true)
        loadItems()
      } else if (ev.kind === 'page_start') {
        setCurrentPage((c) => ({ ...c, [ev.job_id]: ev.page_no }))
      } else if (ev.kind === 'page' || ev.kind === 'job') {
        const job: Job = ev.job
        if (job.folder_id === chatRef.current) {
          if (ev.kind === 'job' && job.status === 'queued' && job.pages_done === 0) {
            // retry-failed removed pages: take the server's copy
            api.job(job.id).then(patchJobFull).catch(() => {})
          } else patchJob(job, ev.page)
        }
        if (job.status !== 'running' && job.status !== 'queued') {
          setCancelling((s) => { const n = new Set(s); n.delete(job.id); return n })
          loadItems()
        }
      } else if (ev.kind === 'model') {
        setModelLoading(ev.state === 'loading')
      }
    }
    es.onerror = () => { /* EventSource reconnects by itself */ }
    es.onopen = () => { if (chatRef.current) loadMessages(chatRef.current) }
    return () => es.close()
  }, [loadItems, loadMessages])

  const createFolder = async () => {
    const name = naming.trim()
    if (!name) return
    try {
      const f = await api.create('folder', name)
      setCreatingFolder(false)
      setNaming('')
      await loadItems()
      setView({ type: 'folder', id: f.id })
    } catch (e) { fail((e as Error).message) }
  }

  const saveRename = async () => {
    if (!renaming) return
    try {
      await api.rename(renaming.id, renaming.name)
      setRenaming(null)
      loadItems()
    } catch (e) { fail((e as Error).message) }
  }

  const moveTo = async (parentId: number | null) => {
    if (!moving) return
    try {
      await api.move(moving.id, parentId)
      if (parentId) setOpen((s) => new Set(s).add(parentId))
      setMoving(null)
      loadItems()
    } catch (e) { fail((e as Error).message) }
  }

  const confirmDelete = async () => {
    if (!deleting) return
    try {
      await api.deleteFolder(deleting.id)
      const gone = new Set([deleting.id, ...items.filter((f) => f.parent_id === deleting.id).map((f) => f.id)])
      if (view.type !== 'new' && gone.has(view.id)) setView({ type: 'new', parentId: null })
      setDeleting(null)
      loadItems()
    } catch (e) { fail((e as Error).message) }
  }

  /** Sends files into the open chat, or starts a chat (named after the first file) and sends them there. */
  const send = async (files: File[], mode: Mode) => {
    try {
      let id = chatId
      if (!id) {
        const parentId = view.type === 'folder' ? view.id : view.type === 'new' ? view.parentId : null
        id = (await api.create('chat', chatName(files), parentId)).id
        await api.upload(id, files, mode)
        await loadItems()
        setView({ type: 'chat', id })
        return
      }
      await api.upload(id, files, mode)
      await loadMessages(id, true)
      loadItems()
    } catch (e) {
      fail((e as Error).message)
      throw e
    }
  }

  const runSearch = async (e?: React.FormEvent) => {
    e?.preventDefault()
    if (view.type === 'new' || !query.trim()) { setHits(null); return }
    try { setHits(await api.search(view.id, query)) } catch (err) { fail((err as Error).message) }
  }

  const folders = items.filter((f) => f.kind === 'folder').sort((a, b) => a.name.localeCompare(b.name, 'th'))
  const looseChats = items.filter((f) => f.kind === 'chat' && !f.parent_id)
  const chatsIn = (id: number) => items.filter((f) => f.kind === 'chat' && f.parent_id === id)
  const parentOf = (f?: Folder) => (f?.parent_id ? items.find((x) => x.id === f.parent_id) : undefined)
  const newParent = view.type === 'new' && view.parentId ? items.find((f) => f.id === view.parentId) : undefined

  const row = (f: Folder, nested = false) => {
    const on = view.type !== 'new' && view.id === f.id
    const isFolder = f.kind === 'folder'
    const expanded = open.has(f.id)
    return (
      <div key={f.id}>
        <div className={`item ${on ? 'on' : ''} ${nested ? 'nested' : ''}`}>
          {renaming?.id === f.id ? (
            <form className="rename" onSubmit={(e) => { e.preventDefault(); saveRename() }}>
              <input autoFocus value={renaming.name} onChange={(e) => setRenaming({ ...renaming, name: e.target.value })}
                onKeyDown={(e) => e.key === 'Escape' && setRenaming(null)} aria-label="ชื่อใหม่" />
              <button className="link">บันทึก</button>
            </form>
          ) : (
            <>
              {isFolder && (
                <button className="twisty" aria-label={expanded ? 'ย่อ' : 'ขยาย'} aria-expanded={expanded}
                  onClick={() => setOpen((s) => { const n = new Set(s); expanded ? n.delete(f.id) : n.add(f.id); return n })}>
                  {expanded ? '▾' : '▸'}
                </button>
              )}
              <button className="item-name" onClick={() => setView(isFolder ? { type: 'folder', id: f.id } : { type: 'chat', id: f.id })}>
                <span aria-hidden>{isFolder ? '📁' : '💬'}</span> <span className="name">{f.name}</span>
                {f.active > 0 && <span className="dot" title="กำลังทำงาน" />}
              </button>
              <span className="item-tools">
                <button className="link" title="เปลี่ยนชื่อ" aria-label="เปลี่ยนชื่อ" onClick={() => setRenaming({ id: f.id, name: f.name })}>✎</button>
                {!isFolder && <button className="link" title="ย้ายไปโฟลเดอร์" aria-label="ย้ายไปโฟลเดอร์" onClick={() => setMoving(f)}>⇄</button>}
                <button className="link danger" title="ลบ" aria-label="ลบ" onClick={() => setDeleting(f)}>🗑</button>
              </span>
            </>
          )}
        </div>
        {isFolder && expanded && (
          <div className="children">
            {chatsIn(f.id).map((c) => row(c, true))}
            <button className="item-add" onClick={() => setView({ type: 'new', parentId: f.id })}>＋ แชตใหม่ในโฟลเดอร์นี้</button>
          </div>
        )}
      </div>
    )
  }

  const searchBox = (placeholder: string) => (
    <form className="search" onSubmit={runSearch}>
      <input type="search" value={query} placeholder={placeholder}
        onChange={(e) => { setQuery(e.target.value); if (!e.target.value) setHits(null) }} aria-label="ค้นหา" />
      <button>ค้นหา</button>
    </form>
  )

  const hitList = hits && (
    <div className="hits">
      <div className="hits-head">
        พบ {hits.length}{hits.length >= 200 ? '+' : ''} หน้าที่มี “{query}”
        <button className="link" onClick={() => setHits(null)}>ปิด</button>
      </div>
      {hits.map((h) => (
        <button key={h.page_id} className="hit" onClick={() => setCompare({ jobId: h.job_id, page: h.page_no })}>
          <div className="hit-title">
            {view.type === 'folder' && <span className="muted">{items.find((f) => f.id === h.chat_id)?.name} › </span>}
            {h.filename} · หน้า {h.page_no} <span className={`badge mode-${h.mode}`}>{MODE_LABEL[h.mode]}</span>
          </div>
          <div className="hit-snippet">{h.snippet.before}<mark>{h.snippet.match}</mark>{h.snippet.after}</div>
        </button>
      ))}
    </div>
  )

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">OCR เอกสาร <span className="muted">OpenThai2.0</span></div>
        <div className="side-actions">
          <button className="primary" onClick={() => setView({ type: 'new', parentId: null })}>＋ แชตใหม่</button>
          <button onClick={() => { setNaming(''); setCreatingFolder(true) }}>＋ โฟลเดอร์ใหม่</button>
        </div>
        <nav className="side-list">
          <div className="side-label">โฟลเดอร์</div>
          {folders.length === 0 && <div className="muted empty-side">ยังไม่มีโฟลเดอร์</div>}
          {folders.map((f) => row(f))}
          <div className="side-label">แชต</div>
          {looseChats.length === 0 && <div className="muted empty-side">ยังไม่มีแชต</div>}
          {looseChats.map((f) => row(f))}
        </nav>
        <ModelControl />
      </aside>

      <main className="chat"
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={(e) => { if (e.currentTarget === e.target) setDragging(false) }}
        onDrop={(e) => { e.preventDefault(); setDragging(false); composer.current?.addFiles(Array.from(e.dataTransfer.files)) }}>

        {view.type === 'new' && (
          <div className="welcome">
            <div className="welcome-inner">
              {newParent && <div className="crumb">📁 {newParent.name}</div>}
              <h1>อัปโหลดเอกสารให้ OpenThai อ่าน</h1>
              <p className="muted">แนบ PDF หรือรูปภาพ เลือกโมเดล แล้วกดส่ง ผลจะขึ้นทีละหน้าและดาวน์โหลดเป็นไฟล์ .txt ได้</p>
              <Composer ref={composer} onSend={send} />
            </div>
          </div>
        )}

        {view.type === 'chat' && current && (
          <>
            <header className="chat-head">
              <h1>
                {parentOf(current) && (
                  <button className="link crumb-link" onClick={() => setView({ type: 'folder', id: current.parent_id! })}>
                    📁 {parentOf(current)!.name} ›
                  </button>
                )}
                {current.name}
              </h1>
              {searchBox('ค้นหาในแชตนี้')}
            </header>
            {hitList}
            <div className="messages">
              {loading && <div className="muted center">กำลังโหลด…</div>}
              {!loading && messages.length === 0 && (
                <div className="empty">
                  <div className="empty-icon" aria-hidden>📎</div>
                  <p>แนบ PDF หรือรูปภาพ แล้วกดส่ง ผลอ่านจะขึ้นที่นี่</p>
                </div>
              )}
              {messages.map((m) => m.type === 'upload' ? (
                <UploadBubble key={m.id} m={m} />
              ) : m.type === 'ocr_result' && m.job ? (
                <ResultCard key={m.id} job={m.job} currentPage={currentPage[m.job.id]}
                  cancelling={cancelling.has(m.job.id)} modelLoading={modelLoading}
                  onCompare={(jobId, page) => setCompare({ jobId, page })} onError={fail}
                  onCancelRequested={(id) => setCancelling((s) => new Set(s).add(id))} />
              ) : null)}
              <div ref={bottom} />
            </div>
            <Composer ref={composer} onSend={send} placeholder="แนบเอกสารเพิ่มในแชตนี้" />
          </>
        )}

        {view.type === 'folder' && current && (
          <>
            <header className="chat-head">
              <h1>📁 {current.name}</h1>
              {searchBox('ค้นหาทุกแชตในโฟลเดอร์')}
            </header>
            {hitList}
            <div className="messages">
              <div className="folder-page">
                <div className="folder-page-head">
                  <span className="muted">{chatsIn(current.id).length} แชต · {current.documents} ไฟล์</span>
                </div>
                {chatsIn(current.id).length === 0 && (
                  <div className="empty">
                    <div className="empty-icon" aria-hidden>📁</div>
                    <p>ยังไม่มีแชตในโฟลเดอร์นี้ แนบเอกสารด้านล่างเพื่อเริ่มแชตแรก</p>
                    <p className="muted">หรือย้ายแชตที่มีอยู่เข้ามาด้วยปุ่ม ⇄ ที่แถบซ้าย</p>
                  </div>
                )}
                {chatsIn(current.id).map((c) => (
                  <button key={c.id} className="chat-row" onClick={() => setView({ type: 'chat', id: c.id })}>
                    <span className="chat-row-name">💬 {c.name} {c.active > 0 && <span className="dot" />}</span>
                    <span className="muted">{c.documents} ไฟล์ · {time(c.updated_at)}</span>
                  </button>
                ))}
              </div>
            </div>
            <Composer ref={composer} onSend={send} placeholder="แนบเอกสารเพื่อเริ่มแชตใหม่ในโฟลเดอร์นี้" />
          </>
        )}

        {dragging && <div className="dropzone">วางไฟล์ที่นี่เพื่อแนบ</div>}
      </main>

      {compare && <Viewer jobId={compare.jobId} page={compare.page} onClose={() => setCompare(null)} />}

      {creatingFolder && (
        <div className="overlay" onClick={() => setCreatingFolder(false)}>
          <form className="dialog" onClick={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); createFolder() }}>
            <h2>สร้างโฟลเดอร์ใหม่</h2>
            <p className="muted">ใช้จัดกลุ่มแชตตามเรื่อง เช่น “สิ่งแวดล้อม” หรือ “กฎหมาย” ค้นหาได้ทุกแชตในโฟลเดอร์</p>
            <input autoFocus className="wide" value={naming} onChange={(e) => setNaming(e.target.value)}
              onKeyDown={(e) => e.key === 'Escape' && setCreatingFolder(false)} placeholder="ชื่อโฟลเดอร์" aria-label="ชื่อโฟลเดอร์" />
            <div className="dialog-actions">
              <button type="button" onClick={() => setCreatingFolder(false)}>ยกเลิก</button>
              <button className="primary" disabled={!naming.trim()}>สร้าง</button>
            </div>
          </form>
        </div>
      )}

      {moving && (
        <div className="overlay" onClick={() => setMoving(null)}>
          <div className="dialog" role="dialog" onClick={(e) => e.stopPropagation()}>
            <h2>ย้าย “{moving.name}” ไปที่</h2>
            <div className="move-list">
              <button className={!moving.parent_id ? 'on' : ''} onClick={() => moveTo(null)}>💬 ไม่อยู่ในโฟลเดอร์</button>
              {folders.map((f) => (
                <button key={f.id} className={moving.parent_id === f.id ? 'on' : ''} onClick={() => moveTo(f.id)}>📁 {f.name}</button>
              ))}
              {folders.length === 0 && <div className="muted">ยังไม่มีโฟลเดอร์ สร้างด้วยปุ่ม “＋ โฟลเดอร์ใหม่”</div>}
            </div>
            <div className="dialog-actions"><button onClick={() => setMoving(null)}>ปิด</button></div>
          </div>
        </div>
      )}

      {deleting && (
        <div className="overlay" onClick={() => setDeleting(null)}>
          <div className="dialog" role="alertdialog" onClick={(e) => e.stopPropagation()}>
            <h2>ลบ{deleting.kind === 'folder' ? 'โฟลเดอร์' : 'แชต'} “{deleting.name}”?</h2>
            <p>
              {deleting.kind === 'folder'
                ? `แชต ${chatsIn(deleting.id).length} แชตในโฟลเดอร์นี้ ไฟล์ ${deleting.documents} ไฟล์ และผล OCR ทั้งหมดจะถูกลบถาวร กู้คืนไม่ได้`
                : `ไฟล์ ${deleting.documents} ไฟล์ ผล OCR และประวัติแชตนี้จะถูกลบถาวร กู้คืนไม่ได้`}
            </p>
            <div className="dialog-actions">
              <button onClick={() => setDeleting(null)}>ยกเลิก</button>
              <button className="danger-btn" onClick={confirmDelete}>ลบถาวร</button>
            </div>
          </div>
        </div>
      )}
      {error && <div className="toast" role="alert">{error}</div>}
    </div>
  )
}

function UploadBubble({ m }: { m: Message }) {
  const p = m.payload as UploadPayload
  return (
    <div className="bubble-row">
      <div className="bubble">
        <div className="bubble-meta">{MODE_LABEL[p.mode]} · {time(m.created_at)}</div>
        {p.files.map((f) => <div key={f.document_id}>📄 {f.filename} <span className="muted">({f.pages} หน้า)</span></div>)}
        {p.rejected.map((r) => <div key={r.filename} className="rejected">✕ {r.filename}: {r.reason}</div>)}
      </div>
    </div>
  )
}

function ModelControl() {
  const [loaded, setLoaded] = useState(false)
  const [msg, setMsg] = useState('')
  const refresh = () => api.status().then((s) => setLoaded(s.markdown_loaded)).catch(() => {})
  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t) }, [])
  if (!loaded) return null
  return (
    <div className="model-control">
      <div className="muted">โมเดล OCR Markdown อยู่ในหน่วยความจำ</div>
      <button onClick={() => api.unloadMarkdown().then(() => { setMsg('ปล่อยหน่วยความจำแล้ว'); refresh() }).catch((e) => setMsg(e.message))}>
        ปล่อยหน่วยความจำ
      </button>
      {msg && <div className="muted">{msg}</div>}
    </div>
  )
}
