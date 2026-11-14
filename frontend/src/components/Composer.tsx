import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import type { Upload } from '../types'

export function Composer() {
  const busy = useStore((s) => s.live !== null)
  const chatId = useStore((s) => s.activeChatId)
  const { send, cancel, setError, queueMessage, unqueue, sendQueued } = useStore.getState()
  const queued = useStore((s) => s.queued)
  const [text, setText] = useState('')
  const [files, setFiles] = useState<Upload[]>([])
  const [uploading, setUploading] = useState(0)
  const [drag, setDrag] = useState(false)
  const area = useRef<HTMLTextAreaElement>(null)
  const picker = useRef<HTMLInputElement>(null)

  useEffect(() => { setText(''); setFiles([]); area.current?.focus() }, [chatId])
  useEffect(() => {
    const el = area.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.4)}px`
  }, [text])

  const add = async (list: FileList | File[]) => {
    for (const f of Array.from(list)) {
      setUploading((n) => n + 1)
      try {
        const up = await api.upload(f, chatId ?? undefined)
        setFiles((prev) => [...prev, up])
      } catch (e) {
        setError(`${f.name}: ${e instanceof Error ? e.message : String(e)}`)
      } finally {
        setUploading((n) => n - 1)
      }
    }
  }

  const canSend = !busy && uploading === 0 && (text.trim() !== '' || files.length > 0)
  // While the agent works, a message is kept for its next step instead of stopping it. Text only: files wait for Send.
  const canQueue = busy && text.trim() !== ''
  const submit = async () => {
    if (canQueue) {
      const t = text
      setText('')
      if (!(await queueMessage(t))) setText((now) => now || t)
      return
    }
    if (!canSend) return
    const t = text, sent = files
    setText('')
    setFiles([])
    if (await send(t, sent.map((f) => f.id))) return
    // Not sent (the banner says why): give the prompt back unless something new was typed meanwhile.
    setText((now) => now || t)
    setFiles((now) => (now.length ? now : sent))
  }

  return (
    <div className="composer">
      <div className="inner">
        <div className={`box${drag ? ' drag' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); if (e.dataTransfer.files.length) void add(e.dataTransfer.files) }}>
          {(files.length > 0 || uploading > 0) && (
            <div className="attachments" style={{ margin: '0 0 6px' }}>
              {files.map((f) => (
                <span key={f.id} className="chip">
                  {f.mime.startsWith('image/') ? '🖼' : '📎'} {f.filename}
                  <button className="icon" aria-label={`Remove ${f.filename}`} onClick={() => setFiles((p) => p.filter((x) => x.id !== f.id))}>✕</button>
                </span>
              ))}
              {uploading > 0 && <span className="chip muted">Uploading…</span>}
            </div>
          )}
          {queued.length > 0 && (
            <div className="queued" aria-label="Messages waiting for the agent">
              <div className="hint">{busy ? 'Waiting for the agent\'s next step. Nothing is interrupted: it reads them when the tool it is running has finished, or they are sent when it ends.'
                : 'The run ended before these were read.'}{!busy && <> <button className="link" onClick={() => void sendQueued()}>Send them now</button></>}</div>
              {queued.map((q) => (
                <div key={q.id} className="queued-item"><span className="grow">{q.text}</span>
                  <button className="icon" aria-label="Remove this waiting message" title="Remove" onClick={() => void unqueue(q.id)}>✕</button></div>
              ))}
            </div>
          )}
          <textarea ref={area} rows={1} value={text} aria-label="Message"
            placeholder={busy ? 'The agent is working — Enter keeps your message for its next step, without stopping it' : 'Message HunterAI — Enter to send, Shift+Enter for a new line'}
            onChange={(e) => setText(e.target.value)}
            onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); void add(e.clipboardData.files) } }}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void submit() } }} />
          <div className="bar">
            <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) void add(e.target.files); e.target.value = '' }} />
            <button className="btn" onClick={() => picker.current?.click()}>Attach</button>
            <span className="note grow">Drop or paste files. Up to 50 MB each.</span>
            {busy
              ? <><button className="btn primary" disabled={!canQueue} title="Keep this message for the agent's next step; it is not interrupted" onClick={submit}>Queue</button>
                <button className="btn danger" title="Stop the run now" onClick={() => cancel()}>Stop</button></>
              : <button className="btn primary" disabled={!canSend} onClick={submit}>Send</button>}
          </div>
        </div>
      </div>
    </div>
  )
}
