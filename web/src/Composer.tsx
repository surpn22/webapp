import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { MODE_HELP, MODE_LABEL, type Mode } from './api'

const ACCEPT = '.pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp'
const OK = /\.(pdf|png|jpe?g|tiff?|webp)$/i
const MODEL_KEY = 'thai-ocr:model'
const MODES: Mode[] = ['text', 'markdown']

const savedMode = (): Mode => {
  try { return localStorage.getItem(MODEL_KEY) === 'markdown' ? 'markdown' : 'text' } catch { return 'text' }
}

export interface ComposerHandle { addFiles: (files: File[]) => void }

const size = (b: number) => (b > 1 << 20 ? `${(b / (1 << 20)).toFixed(1)} MB` : `${Math.ceil(b / 1024)} KB`)

export const Composer = forwardRef<ComposerHandle, {
  disabled?: boolean
  placeholder?: string
  onSend: (files: File[], mode: Mode) => Promise<void>
}>(function Composer({ disabled, placeholder, onSend }, ref) {
  const [files, setFiles] = useState<File[]>([])
  const [mode, setModeState] = useState<Mode>(savedMode)
  const [menu, setMenu] = useState(false)
  const [sending, setSending] = useState(false)
  const [skipped, setSkipped] = useState<string[]>([])
  const input = useRef<HTMLInputElement>(null)
  const picker = useRef<HTMLDivElement>(null)

  const setMode = (m: Mode) => {
    setModeState(m)
    setMenu(false)
    try { localStorage.setItem(MODEL_KEY, m) } catch { /* no storage */ }
  }

  useEffect(() => {
    if (!menu) return
    const close = (e: MouseEvent) => { if (!picker.current?.contains(e.target as Node)) setMenu(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [menu])

  const addFiles = (incoming: File[]) => {
    setSkipped(incoming.filter((f) => !OK.test(f.name)).map((f) => f.name))
    setFiles((cur) => [...cur, ...incoming.filter((f) => OK.test(f.name))])
  }
  useImperativeHandle(ref, () => ({ addFiles }))

  const send = async () => {
    if (!files.length || sending) return
    setSending(true)
    try {
      await onSend(files, mode)
      setFiles([])
      setSkipped([])
    } catch { /* the caller shows the error; keep the files so the user can try again */ } finally {
      setSending(false)
    }
  }

  return (
    <div className="composer">
      <div className="composer-box">
        {files.length > 0 ? (
          <div className="chips">
            {files.map((f, i) => (
              <span className="chip" key={i}>
                📄 {f.name} <span className="muted">{size(f.size)}</span>
                <button className="link" aria-label={`เอา ${f.name} ออก`} onClick={() => setFiles(files.filter((_, j) => j !== i))}>✕</button>
              </span>
            ))}
          </div>
        ) : (
          <button className="composer-placeholder" onClick={() => input.current?.click()} disabled={disabled}>
            {placeholder ?? 'แนบเอกสาร PDF หรือรูปภาพ ให้โมเดลอ่าน (ลากไฟล์มาวางได้)'}
          </button>
        )}
        {skipped.length > 0 && (
          <div className="page-warn">ข้ามไฟล์ที่ไม่รองรับ: {skipped.join(', ')} (รับ PDF, PNG, JPG, TIFF, WEBP)</div>
        )}
        <div className="composer-row">
          <button className="icon-btn" title="แนบไฟล์" aria-label="แนบไฟล์" onClick={() => input.current?.click()} disabled={disabled}>📎</button>
          <input ref={input} type="file" multiple accept={ACCEPT} hidden
            onChange={(e) => { addFiles(Array.from(e.target.files ?? [])); e.target.value = '' }} />
          <span className="spacer" />
          <div className="model-picker" ref={picker}>
            <button className="model-btn" aria-haspopup="listbox" aria-expanded={menu} onClick={() => setMenu(!menu)}>
              {MODE_LABEL[mode]} <span aria-hidden>▾</span>
            </button>
            {menu && (
              <div className="model-menu" role="listbox" aria-label="เลือกโมเดล">
                {MODES.map((m) => (
                  <button key={m} role="option" aria-selected={mode === m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>
                    <span className="model-name">OpenThai2.0 {MODE_LABEL[m]} {mode === m && <span className="check">✓</span>}</span>
                    <span className="model-desc">{MODE_HELP[m]}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button className="primary send" onClick={send} disabled={disabled || sending || !files.length}
            aria-label="ส่ง" title="ส่ง">
            {sending ? '…' : '↑'}
          </button>
        </div>
      </div>
    </div>
  )
})
