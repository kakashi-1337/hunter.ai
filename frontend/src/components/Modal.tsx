import { useEffect, useRef, type ReactNode } from 'react'
import { ErrorBoundary } from './ErrorBoundary'

export function Modal({ title, onClose, children, footer, wide }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  // A press on the page behind closes the dialog only while nothing was put into it: a stray click must not
  // throw away a pasted brief or a half-filled form. Escape and the dialog's own buttons close it either way.
  const touched = useRef(false)
  const touch = () => { touched.current = true }
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && !touched.current && onClose()}>
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
