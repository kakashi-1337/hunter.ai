import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import { appendTail } from '../tree'
import type { ProcessInfo } from '../types'
import { Modal } from './Modal'

const label = (p: ProcessInfo) => (p.status === 'exited' ? `exited (code ${p.exit_code})` : p.status)

/** The chat's processes: output tail, input, and kill. Output is plain text. */
export function ProcessPanel({ onClose }: { onClose: () => void }) {
  const chatId = useStore((s) => s.chat?.id)
  const setError = useStore((s) => s.setError)
  const [procs, setProcs] = useState<ProcessInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [output, setOutput] = useState('')
  const [input, setInput] = useState('')
  const offset = useRef(0)
  const lastStatus = useRef<string | null>(null)

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e))
  /** The reaper closes a background process's step without an event, so refetch the run shown. */
  const refreshRun = () => {
    const s = useStore.getState()
    if (s.selectedRunId && !s.live) void s.selectRun(s.selectedRunId)
  }
  const reload = async () => {
    if (!chatId) return
    try {
      const list = await api.listProcesses(chatId)
      setProcs(list)
      setSelected((cur) => cur ?? list[0]?.id ?? null)
    } catch (e) { fail(e) }
  }
  useEffect(() => { void reload() }, [chatId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selected) return
    offset.current = 0
    lastStatus.current = null
    setOutput('')
    let stop = false
    const poll = async () => {
      try {
        const r = await api.processOutput(selected, offset.current)
        if (stop) return
        offset.current = r.next_offset
        if (r.text) setOutput((prev) => appendTail(prev, r.text))
        if (lastStatus.current === 'running' && r.status !== 'running') { await reload(); refreshRun() }
        lastStatus.current = r.status
        if (r.status === 'running' || r.next_offset < r.total) timer = setTimeout(poll, r.next_offset < r.total ? 50 : 1000)
      } catch (e) { fail(e) }
    }
    let timer = setTimeout(poll, 0)
    return () => { stop = true; clearTimeout(timer) }
  }, [selected]) // eslint-disable-line react-hooks/exhaustive-deps

  const current = procs.find((p) => p.id === selected)
  const send = async (eof: boolean) => {
    if (!selected) return
    try { await api.processInput(selected, eof ? '' : input + '\n', eof); setInput('') } catch (e) { fail(e) }
  }
  const kill = async () => {
    if (!selected) return
    try { await api.processKill(selected); await reload(); refreshRun() } catch (e) { fail(e) }
  }

  return (
    <Modal title="Processes of this chat" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
      {procs.length === 0 ? <div className="muted">No process has been started in this chat.</div> : (
        <div className="procs">
          <ul className="proc-list">
            {procs.map((p) => (
              <li key={p.id}>
                <button className={p.id === selected ? 'on' : ''} onClick={() => setSelected(p.id)}>
                  <span className={`dot ${p.status === 'running' ? 'running' : p.status === 'exited' && p.exit_code === 0 ? 'done' : 'error'}`} />
                  <span className="mono cmd">{p.command}</span>
                  <span className="muted">{label(p)}</span>
                </button>
              </li>
            ))}
          </ul>
          {current && (
            <div className="proc-view">
              <div className="hint mono">cwd: {current.cwd}{current.pid ? ` · pid ${current.pid}` : ''} · {label(current)}</div>
              <pre className="proc-out" aria-label="Process output">{output || '(no output yet)'}</pre>
              {current.status === 'running' && (
                <div className="row">
                  {current.background && (
                    <>
                      <input className="grow" aria-label="Input for the process" value={input} placeholder="Type a line and press Enter"
                        onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void send(false) }} />
                      <button className="btn" onClick={() => send(false)}>Send</button>
                      <button className="btn" onClick={() => send(true)}>Send EOF</button>
                    </>
                  )}
                  <button className="btn danger" onClick={kill}>Kill</button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
