import { useEffect, useRef, useState, type ReactNode } from 'react'
import { pressBehindCloses } from '../tree'
import { ErrorBoundary } from './ErrorBoundary'

export function Modal({ title, onClose, children, footer, wide }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  // A press on the page behind closes the dialog only while nothing was put into it: a stray click must not
  // throw away a pasted brief or a half-filled form. Nor does it close a dialog that has only just appeared:
  // the second press of a double click on the button that opened it lands on the page behind, and the dialog
  // was gone as soon as it came. Escape and the dialog's own buttons close it either way.
  const touched = useRef(false)
  const touch = () => { touched.current = true }
  const [shownAt] = useState(() => Date.now())       // once, when the dialog appears: not at each drawing
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && !touched.current && pressBehindCloses(shownAt, Date.now()) && onClose()}>
      <div className={`modal${wide ? ' wide' : ''}`} role="dialog" aria-modal="true" aria-label={title} onInput={touch} onChange={touch} onDrop={touch}>
        <h2>{title}</h2>
        <div className="body"><ErrorBoundary>{children}</ErrorBoundary></div>
        {footer && <div className="foot">{footer}</div>}
      </div>
    </div>
  )
}

/** Around a dialog that is opened from the page: when the dialog fails before it has drawn anything, the failure is
 * shown as a dialog of the same name that can be closed, and the page behind it stays as it is. */
export function DialogGuard({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <ErrorBoundary around={(message) => (
      <Modal title={title} onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Close</button>}>{message}</Modal>
    )}>{children}</ErrorBoundary>
  )
}
