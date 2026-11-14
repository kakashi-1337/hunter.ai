import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'
import { APPROVE_DELAY_MS, canApprove, gateLine, visible } from '../tree'
import type { PermissionDecision, PermissionRequest } from '../types'

/**
 * The exact call a tool wants to make, for the owner to allow or deny. Rendered with
 * key={request.id}, so each request is a fresh component: focus starts on Deny, no key press
 * approves, and the Allow buttons stay disabled briefly so a double click cannot land on the
 * next request.
 */
export function PermissionPrompt({ request, waiting }: { request: PermissionRequest; waiting: number }) {
  const answer = useStore((s) => s.answer)
  const [note, setNote] = useState('')
  const [ready, setReady] = useState(false)
  const [sent, setSent] = useState(false)
  const shownAt = useRef(Date.now())
  const pressed = useRef<string | null>(null)
  const deny = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    deny.current?.focus()
    const t = setTimeout(() => setReady(true), APPROVE_DELAY_MS)
    return () => clearTimeout(t)
  }, [])

  const reply = (decision: PermissionDecision) => {
    // Only a click that began on this very request counts.
    if (sent || pressed.current !== request.id) return
    if (decision !== 'deny' && !canApprove(shownAt.current, Date.now())) return
    setSent(true)
    void answer(request.id, decision, note)
  }
  const press = () => { pressed.current = request.id }
  const args = visible(JSON.stringify(request.arguments, null, 2))

  return (
    <section className="ask" role="alertdialog" aria-label="Permission request">
      <div className="ask-head">
        <b>Allow this call?</b>
        <span className="mono">{request.tool}</span>
        <span className="badge">{request.risk}</span>
        <span className="muted">{request.source}</span>
        {waiting > 1 && <span className="badge warn">{waiting - 1} more waiting</span>}
      </div>
      {request.warnings.map((w) => <div key={w} className="ask-warning" role="alert">{w}</div>)}
      <pre className="ask-args">{args}</pre>
      {args.length > 600 && <div className="ask-size">{args.length.toLocaleString()} characters in all. Scroll this box to see every one before you answer.</div>}
      <dl className="ask-facts">
        {request.cwd && (<><dt>Working folder</dt><dd className="mono">{visible(request.cwd)}</dd></>)}
        {request.paths.map((p) => (
          <div key={`${p.mode}:${p.path}`} className="fact"><dt>{p.mode === 'write' ? 'Writes' : 'Reads'}</dt><dd className="mono">{visible(p.path)}</dd></div>
        ))}
        {request.hosts.map((h) => (
          <div key={`${h.host}:${h.port}`} className="fact">
            <dt>Host</dt>
            <dd className="mono">{h.host}{h.port ? `:${h.port}` : ''} ({h.ips.length ? h.ips.join(', ') : 'not resolved yet'})</dd>
          </div>
        ))}
      </dl>
      {request.preview && <pre className="ask-preview">{visible(request.preview)}</pre>}
      {gateLine(request.gate) && (
        <div className={`ask-gate${request.gate?.clears ? '' : ' warn-line'}`}>{visible(gateLine(request.gate)!)}</div>
      )}
      <ul className="ask-reasons">{request.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      <div className="row">
        <input className="grow" placeholder="Note for the model (optional)" value={note} maxLength={2000}
          onChange={(e) => setNote(e.target.value)} />
        <button ref={deny} className="btn danger" disabled={sent} onPointerDown={press} onKeyDown={press} onClick={() => reply('deny')}>Deny</button>
        <button className="btn" disabled={sent || !ready} onPointerDown={press} onKeyDown={press} onClick={() => reply('allow_once')}>Allow once</button>
        <button className="btn" disabled={sent || !ready || !request.can_allow_chat} onPointerDown={press} onKeyDown={press}
          title={request.can_allow_chat ? `Also allows later calls of ${request.tool.startsWith('browser_') ? 'the browser tools (open, click, type)' : request.tool} with this exact target:\n${request.grant_target}` : 'Not available for this call'}
          onClick={() => reply('allow_chat')}>Allow for this chat</button>
      </div>
      {request.can_allow_chat && (
        <div className="ask-covers">"Allow for this chat" also allows every later call of <span className="mono">{request.tool.startsWith('browser_') ? 'the browser tools (open, click, type)' : request.tool}</span> with this target: <span className="mono">{visible(request.grant_target)}</span></div>
      )}
    </section>
  )
}
