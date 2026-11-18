import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useMemo, useState } from 'react'
import type { Line, Page } from './api'

/** Lines kept by the model but below this confidence are marked for checking. */
export const LOW_CONFIDENCE = 0.9

interface Segment { text: string; conf?: number }

/** Splits the raw text around low-confidence lines without changing a single character. */
function segments(content: string, lines: Line[]): Segment[] {
  const out: Segment[] = []
  let cur = 0
  for (const l of lines) {
    if (l.confidence >= LOW_CONFIDENCE || !l.text) continue
    const i = content.indexOf(l.text, cur)
    if (i < 0) continue
    if (i > cur) out.push({ text: content.slice(cur, i) })
    out.push({ text: l.text, conf: l.confidence })
    cur = i + l.text.length
  }
  out.push({ text: content.slice(cur) })
  return out
}

function renderMarkdown(src: string): string {
  const html = marked.parse(src, { async: false, gfm: true, breaks: true }) as string
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } })
}

export function PageContent({ page, raw }: { page: Page; raw?: boolean }) {
  const [showDropped, setShowDropped] = useState(false)
  const lines = page.meta?.lines ?? []
  const dropped = page.meta?.dropped ?? []
  const lowCount = lines.filter((l) => l.confidence < LOW_CONFIDENCE).length
  const html = useMemo(
    () => (page.mode === 'markdown' && !raw ? renderMarkdown(page.content) : ''),
    [page.content, page.mode, raw])

  if (page.status === 'failed') {
    return <div className="page-error">อ่านหน้านี้ไม่สำเร็จ: {page.error}</div>
  }

  return (
    <div className="page-content">
      {page.status === 'incomplete' && (
        <div className="page-warn">โมเดลอ่านหน้านี้ไม่จบ ผลด้านล่างคือสิ่งที่โมเดลให้มา ควรเทียบกับต้นฉบับ</div>
      )}
      {page.mode === 'markdown' && !raw ? (
        <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <pre className="raw">
          {page.mode === 'text'
            ? segments(page.content, lines).map((s, i) =>
                s.conf !== undefined ? (
                  <mark key={i} className="low" title={`ความมั่นใจ ${(s.conf * 100).toFixed(0)}% ควรตรวจกับต้นฉบับ`}>
                    {s.text}
                  </mark>
                ) : (
                  <span key={i}>{s.text}</span>
                ))
            : page.content}
        </pre>
      )}
      {page.mode === 'text' && (lowCount > 0 || dropped.length > 0) && (
        <div className="page-notes">
          {lowCount > 0 && <span><mark className="low">ขีดเส้นสีส้ม</mark> = บรรทัดที่ความมั่นใจต่ำกว่า {LOW_CONFIDENCE * 100}% ({lowCount} บรรทัด)</span>}
          {dropped.length > 0 && (
            <button className="link" onClick={() => setShowDropped(!showDropped)}>
              โมเดลตัดทิ้ง {dropped.length} บรรทัดเพราะไม่มั่นใจ {showDropped ? '▴' : '▾'}
            </button>
          )}
        </div>
      )}
      {showDropped && (
        <ul className="dropped">
          <li className="dropped-note">บรรทัดเหล่านี้ไม่อยู่ในผลลัพธ์ (เป็นค่าตั้งต้นของโมเดล มักเป็นตราประทับ ลายเซ็น หรือลายมือ)</li>
          {dropped.map((d, i) => (
            <li key={i}><span className="conf">{(d.confidence * 100).toFixed(0)}%</span> {d.text || '(ว่าง)'}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
