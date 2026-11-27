import { useEffect, useRef, useState } from 'react'
import { answer, useDialog } from '../dialog'
import { pressBehindCloses } from '../tree'

/** Shows the question that confirmBox() or promptBox() asked. Mounted once, above every other dialog. */
export function DialogHost() {
  const current = useDialog((s) => s.current)
  const [value, setValue] = useState('')
  const ok = useRef<HTMLButtonElement>(null)
  const field = useRef<HTMLInputElement>(null)
  const typed = useRef(false)      // a press on the page behind does not throw away an answer that was started
  const shownAt = useRef(0)        // nor does it answer a question that has only just been asked (see Modal)
  useEffect(() => {
    if (!current) return
    typed.current = false
    shownAt.current = Date.now()
    setValue(current.initial)
    // Focus after paint: Enter then answers, and Escape cancels, without reaching the dialog underneath.
    const t = setTimeout(() => (current.kind === 'prompt' ? field.current : ok.current)?.focus(), 0)
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); answer(current.kind === 'prompt' ? null : false) } }
    window.addEventListener('keydown', onKey, true)
    return () => { clearTimeout(t); window.removeEventListener('keydown', onKey, true) }
  }, [current])
  if (!current) return null
  const cancel = () => answer(current.kind === 'prompt' ? null : false)
  const accept = () => answer(current.kind === 'prompt' ? value : true)
  return (
    <div className="overlay dialog-host" onMouseDown={(e) => e.target === e.currentTarget && !typed.current && pressBehindCloses(shownAt.current, Date.now()) && cancel()}>
      <form className="modal dialog" role="alertdialog" aria-modal="true" aria-label={current.message} onInput={() => { typed.current = true }}
        onSubmit={(e) => { e.preventDefault(); accept() }}>
        <div className="body">
          <div className="dialog-message">{current.message}</div>
          {current.kind === 'prompt' && <input ref={field} value={value} aria-label={current.message} onChange={(e) => setValue(e.target.value)} />}
        </div>
        <div className="foot">
          <button type="button" className="btn" onClick={cancel}>Cancel</button>
          <button ref={ok} type="submit" className={`btn ${current.danger ? 'danger-solid' : 'primary'}`}>{current.kind === 'prompt' ? 'OK' : current.danger ? 'Yes, do it' : 'OK'}</button>
        </div>
      </form>
    </div>
  )
}
