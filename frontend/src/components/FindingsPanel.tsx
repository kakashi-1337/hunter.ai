import { useEffect, useMemo, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import { copyText } from './Markdown'
import { filterFindings, findingsMarkdown, triageFacts, visible } from '../tree'
import type { FindingRow, FindingStatus, Severity } from '../types'
import { Modal } from './Modal'

const SEVERITIES: Severity[] = ['critical', 'high', 'medium', 'low', 'info']
const STATUSES: FindingStatus[] = ['open', 'confirmed', 'false_positive', 'duplicate', 'reported', 'fixed']
const text = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** Every finding of every target in one place, with filters and a Markdown export of what is shown. */
export function FindingsPanel({ onClose }: { onClose: () => void }) {
  const openChat = useStore((s) => s.openChat)
  const [all, setAll] = useState<FindingRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [severity, setSeverity] = useState('')
  const [status, setStatus] = useState('')
  const [target, setTarget] = useState('')
  const [query, setQuery] = useState('')
  const [copied, setCopied] = useState(false)
  useEffect(() => { api.allFindings().then(setAll).catch((e) => setError(text(e))) }, [])

  const shown = useMemo(() => filterFindings(all ?? [], { severity, status, target_id: target, text: query }), [all, severity, status, target, query])
  const targets = useMemo(() => [...new Map((all ?? []).map((f) => [f.target_id, f.target_name])).entries()].sort((a, b) => a[1].localeCompare(b[1])), [all])
  const patch = async (f: FindingRow, change: Parameters<typeof api.patchFinding>[1]) => {
    try {
      const next = await api.patchFinding(f.id, change)
      setAll((list) => (list ?? []).map((x) => (x.id === f.id ? { ...x, ...next } : x)))
    } catch (e) { setError(text(e)) }
  }
  const remove = async (f: FindingRow) => {
    if (!window.confirm(`Delete the finding "${f.title}"?`)) return
    try { await api.deleteFinding(f.id); setAll((list) => (list ?? []).filter((x) => x.id !== f.id)) } catch (e) { setError(text(e)) }
  }
  const download = () => {
    const url = URL.createObjectURL(new Blob([findingsMarkdown(shown)], { type: 'text/markdown' }))
    const a = document.createElement('a')
    a.href = url; a.download = `findings-${new Date().toISOString().slice(0, 10)}.md`; a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const copy = async () => { setCopied(await copyText(findingsMarkdown(shown))); setTimeout(() => setCopied(false), 1200) }
  const counts = SEVERITIES.map((s) => [s, shown.filter((f) => f.severity === s).length] as const).filter(([, n]) => n)

  return (
    <Modal wide title="Findings" onClose={onClose} footer={<>
      <span className="hint grow">The export holds what is shown, evidence included: read it before you send it anywhere.</span>
      <button className="btn" disabled={!shown.length} onClick={() => void copy()}>{copied ? 'Copied' : 'Copy as Markdown'}</button>
      <button className="btn" disabled={!shown.length} onClick={download}>Download .md</button>
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      <div className="row findings-filters">
        <input className="grow" type="search" aria-label="Search findings" placeholder="Search titles and evidence" value={query} onChange={(e) => setQuery(e.target.value)} />
        <select aria-label="Target filter" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">All targets</option>{targets.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
        <select aria-label="Severity filter" value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">Any severity</option>{SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}</select>
        <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option>{STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}</select>
      </div>
      {error && <div className="ask-warning" role="alert">{error}</div>}
      {all === null && !error && <div className="hint">Loading…</div>}
      {all !== null && (<div className="row">
        <span className="hint">{shown.length} of {all.length} shown</span>
        {counts.map(([s, n]) => <span key={s} className={`badge sev-${s}`}>{n} {s}</span>)}
      </div>)}
      {all !== null && all.length === 0 && <div className="hint">No findings yet. The agent records one with the finding_add tool when a chat has a target; you can also ask it to.</div>}
      {all !== null && all.length > 0 && shown.length === 0 && <div className="hint">Nothing matches these filters.</div>}
      {shown.map((f) => (
        <div key={f.id} className="finding" data-severity={f.severity}>
          <div className="row">
            <span className={`badge sev-${f.severity}`}>{f.severity}</span>
            <b className="grow">{visible(f.title)}</b>
            <select aria-label="Severity" value={f.severity} onChange={(e) => void patch(f, { severity: e.target.value as Severity })}>
              {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}</select>
            <select aria-label="Status" value={f.status} onChange={(e) => void patch(f, { status: e.target.value as FindingStatus })}>
              {STATUSES.map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}</select>
            <button className="icon" aria-label="Delete finding" title="Delete" onClick={() => void remove(f)}>✕</button>
          </div>
          <div className="hint">{visible(f.target_name)} · {f.created_at.slice(0, 10)}
            {f.chat_id && f.chat_title !== null && <> · <button className="link" onClick={() => { void openChat(f.chat_id!); onClose() }}>{visible(f.chat_title)}</button></>}</div>
          {triageFacts(f.triage).length > 0 && <div className="hint">Model's triage (not a fact): {triageFacts(f.triage).join(' · ')}</div>}
          {f.evidence && <details><summary>Evidence</summary><pre>{visible(f.evidence)}</pre></details>}
        </div>
      ))}
    </Modal>
  )
}
