import { useState } from 'react'
import { api, downloadUrl, jobText, MODE_LABEL, type Job } from './api'
import { PageContent } from './PageContent'

const STATUS: Record<Job['status'], string> = {
  queued: 'รอคิว',
  running: 'กำลังอ่าน',
  done: 'เสร็จแล้ว',
  cancelled: 'ยกเลิกแล้ว',
  interrupted: 'ถูกขัดจังหวะ (แอปถูกปิดระหว่างทำ)',
  failed: 'ล้มเหลว',
}

async function copy(text: string, done: () => void) {
  await navigator.clipboard.writeText(text)
  done()
}

export function ResultCard(props: {
  job: Job
  currentPage?: number
  cancelling?: boolean
  modelLoading?: boolean
  onCompare: (jobId: number, page: number) => void
  onError: (msg: string) => void
  onCancelRequested: (jobId: number) => void
}) {
  const { job, currentPage, cancelling, modelLoading } = props
  const [raw, setRaw] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  const pages = job.pages ?? []
  const doc = job.document
  const active = job.status === 'queued' || job.status === 'running'
  const pct = job.page_total ? Math.round((job.pages_done / job.page_total) * 100) : 0
  const okPages = pages.filter((p) => p.seconds)
  const avg = okPages.length ? okPages.reduce((a, p) => a + (p.seconds ?? 0), 0) / okPages.length : 0
  const left = job.page_total - job.pages_done
  const problemPages = pages.filter((p) => p.status !== 'ok').map((p) => p.page_no)
  const otherMode = job.mode === 'text' ? 'markdown' : 'text'

  const act = (fn: () => Promise<unknown>) => fn().catch((e) => props.onError(e.message))
  const flash = (what: string) => { setCopied(what); setTimeout(() => setCopied(null), 1500) }

  return (
    <div className={`card status-${job.status}`} id={`job-${job.id}`}>
      <div className="card-head">
        <div className="card-title">
          <span className="file-icon" aria-hidden>📄</span>
          <span className="filename" title={doc?.filename}>{doc?.filename}</span>
          <span className={`badge mode-${job.mode}`}>{MODE_LABEL[job.mode]}</span>
        </div>
        <div className={`status status-${job.status}`}>
          {cancelling && active ? 'กำลังยกเลิก (รอหน้าปัจจุบันเสร็จ)' : STATUS[job.status]}
        </div>
      </div>

      <div className="progress-row">
        <div className="progress"><div className="bar" style={{ width: `${pct}%` }} /></div>
        <span className="progress-text">
          {job.pages_done}/{job.page_total} หน้า
          {active && currentPage ? ` · กำลังอ่านหน้า ${currentPage}` : ''}
          {active && avg > 0 && left > 0 ? ` · เหลืออีกราว ${Math.ceil((avg * left) / 60)} นาที` : ''}
          {!active && avg > 0 ? ` · เฉลี่ย ${avg.toFixed(1)} วินาที/หน้า` : ''}
        </span>
      </div>
      {active && modelLoading && job.mode === 'markdown' && pages.length === 0 && (
        <div className="info">กำลังโหลดโมเดล OCR Markdown (2.5 GB) ครั้งแรกใช้เวลาสักครู่</div>
      )}
      {problemPages.length > 0 && (
        <div className="page-error">หน้าที่มีปัญหา: {problemPages.join(', ')}</div>
      )}
      {job.error && <div className="page-error">{job.error}</div>}

      <div className="actions">
        {active && !cancelling && (
          <button onClick={() => { props.onCancelRequested(job.id); act(() => api.cancel(job.id)) }}>ยกเลิก</button>
        )}
        {(job.status === 'interrupted' || job.status === 'cancelled' || job.status === 'failed') &&
          job.pages_done < job.page_total && (
          <button className="primary" onClick={() => act(() => api.resume(job.id))}>
            ทำต่อ ({job.page_total - job.pages_done} หน้าที่เหลือ)
          </button>
        )}
        {!active && problemPages.length > 0 && (
          <button onClick={() => act(() => api.retryFailed(job.id))}>อ่านหน้าที่มีปัญหาใหม่</button>
        )}
        {pages.length > 0 && (
          <>
            <button onClick={() => copy(jobText(job), () => flash('all'))}>{copied === 'all' ? 'คัดลอกแล้ว ✓' : 'คัดลอกทั้งหมด'}</button>
            <a className="button primary" href={downloadUrl(job.id, 'txt')}>⬇ ดาวน์โหลด .txt</a>
            <a className="button" href={downloadUrl(job.id, 'md')}>ดาวน์โหลด .md</a>
            {job.mode === 'markdown' && (
              <button onClick={() => setRaw(!raw)}>{raw ? 'ดูแบบจัดรูปแบบ' : 'ดูผลดิบ'}</button>
            )}
          </>
        )}
        {!active && doc && (
          <button onClick={() => act(() => api.rerun(doc.id, otherMode))}>รันใหม่ด้วย {MODE_LABEL[otherMode]}</button>
        )}
        {pages.length > 1 && (
          <button className="link" onClick={() => setCollapsed(!collapsed)}>{collapsed ? 'แสดงผล' : 'ซ่อนผล'}</button>
        )}
      </div>

      {!collapsed && pages.map((p) => (
        <section key={p.id} className="page" id={`job-${job.id}-page-${p.page_no}`}>
          <div className="page-head">
            <span>หน้า {p.page_no}{p.seconds ? <span className="muted"> · {p.seconds.toFixed(1)} วินาที</span> : null}</span>
            <span className="page-actions">
              <button className="link" onClick={() => copy(p.content, () => flash(`p${p.id}`))}>
                {copied === `p${p.id}` ? 'คัดลอกแล้ว ✓' : 'คัดลอกหน้านี้'}
              </button>
              <button className="link" onClick={() => props.onCompare(job.id, p.page_no)}>เทียบกับต้นฉบับ</button>
            </span>
          </div>
          <PageContent page={p} raw={raw} />
        </section>
      ))}
    </div>
  )
}
