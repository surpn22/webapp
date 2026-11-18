import { useEffect, useState } from 'react'
import { api, MODE_LABEL, pageImageUrl, type Job } from './api'
import { PageContent } from './PageContent'

/** Original page and its OCR side by side, for checking by eye. */
export function Viewer({ jobId, page, onClose }: { jobId: number; page: number; onClose: () => void }) {
  const [job, setJob] = useState<Job | null>(null)
  const [n, setN] = useState(page)
  const [raw, setRaw] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { api.job(jobId).then(setJob).catch((e) => setError(e.message)) }, [jobId])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (!job) return
      if (e.key === 'ArrowRight') setN((x) => Math.min(job.page_total, x + 1))
      if (e.key === 'ArrowLeft') setN((x) => Math.max(1, x - 1))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [job, onClose])

  const p = job?.pages?.find((x) => x.page_no === n)
  return (
    <div className="overlay" onClick={onClose}>
      <div className="viewer" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="เทียบกับต้นฉบับ">
        <div className="viewer-head">
          <strong className="filename">{job?.document?.filename}</strong>
          {job && <span className={`badge mode-${job.mode}`}>{MODE_LABEL[job.mode]}</span>}
          <span className="spacer" />
          {job && (
            <span className="pager">
              <button disabled={n <= 1} onClick={() => setN(n - 1)}>‹</button>
              หน้า {n}/{job.page_total}
              <button disabled={n >= job.page_total} onClick={() => setN(n + 1)}>›</button>
            </span>
          )}
          {job?.mode === 'markdown' && <button onClick={() => setRaw(!raw)}>{raw ? 'จัดรูปแบบ' : 'ผลดิบ'}</button>}
          <button onClick={onClose} aria-label="ปิด">✕</button>
        </div>
        {error && <div className="page-error">{error}</div>}
        {job && (
          <div className="viewer-body">
            <div className="viewer-img">
              <img src={pageImageUrl(job.document_id, n)} alt={`ต้นฉบับหน้า ${n}`} />
            </div>
            <div className="viewer-text">
              {p ? <PageContent page={p} raw={raw} /> : <div className="muted">หน้านี้ยังไม่มีผล OCR</div>}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
