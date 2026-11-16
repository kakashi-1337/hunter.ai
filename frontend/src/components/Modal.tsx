import { useEffect, useRef, type ReactNode } from 'react'

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
        <div className="body">{children}</div>
        {footer && <div className="foot">{footer}</div>}
      </div>
    </div>
  )
}
