import { useState } from 'react'
import { formatDuration, swarmMembers, visible, type CardStatus, type ToolCard as Card } from '../tree'
import type { Step, SwarmMember } from '../types'

const LABEL: Record<CardStatus, string> = {
  waiting: 'waiting for your answer', running: 'running', done: 'done', error: 'failed or denied', queued: 'queued',
  stopped: 'not finished: the run stopped',
}

const MEMBER: Record<SwarmMember['state'], string> = { queued: 'waiting for a place', running: 'working', done: 'finished', error: 'failed', cancelled: 'stopped' }

/** The subagents of a swarm call, one line each: what it was asked, where it is, and how its answer was judged. */
function Swarm({ members }: { members: SwarmMember[] }) {
  const done = members.filter((m) => m.state !== 'queued' && m.state !== 'running').length
  const verified = members.filter((m) => m.verified).length
  return (
    <div className="swarm" aria-label="Subagents of this swarm">
      <div className="hint">{done} of {members.length} finished{done ? ` · ${verified} verified against their own tool outputs` : ''}</div>
      {members.map((m) => (
        <div key={m.n} className="swarm-row">
          <div className="row">
            <span className={`dot ${m.state === 'queued' ? '' : m.state}`} title={MEMBER[m.state]} />
            <b>{m.n}</b>
            <span className="grow swarm-brief" title={visible(m.brief)}>{visible(m.brief) || MEMBER[m.state]}</span>
            {(m.turns > 0 || m.calls > 0) && <span className="muted mono">{m.turns} turns · {m.calls} calls</span>}
            {m.ms !== undefined && <span className="muted mono">{formatDuration(m.ms)}</span>}
            {m.state === 'queued' || m.state === 'running'
              ? <span className={`badge ${m.state === 'running' ? '' : 'muted'}`}>{MEMBER[m.state]}</span>
              : m.verified ? <span className="badge ok">verified</span>
                : <span className={`badge ${m.state === 'done' ? 'warn' : 'bad'}`}>{m.state === 'done' ? (m.verdict && m.verdict !== 'unverified' ? visible(m.verdict) : 'not verified') : MEMBER[m.state]}</span>}
          </div>
          {m.answer && <details><summary>{m.state === 'done' ? 'Its answer' : 'What went wrong'}{m.state === 'done' && !m.verified ? ' (a claim, not checked)' : ''}</summary><pre className="tool-result">{visible(m.answer)}</pre></details>}
        </div>
      ))}
    </div>
  )
}

/** One tool call. Arguments and results are untrusted: plain text only, never Markdown or HTML. */
export function ToolCard({ card, status, durationMs, steps }: { card: Card; status: CardStatus; durationMs?: number; steps?: Step[] }) {
  const [open, setOpen] = useState(false)
  const tasks = card.call.arguments?.tasks
  const members = card.call.name !== 'swarm' ? [] : swarmMembers(steps ?? [], card.call.id, card.result && !card.result.is_error ? card.result.content : null)
    .map((m) => { const t = Array.isArray(tasks) ? tasks[m.n - 1] : null; return m.brief || typeof t?.brief !== 'string' ? m : { ...m, brief: t.brief } })
  const args = visible(JSON.stringify(card.call.arguments, null, 2))
  return (
    <div className={`tool-card ${status}`}>
      <div className="tool-head">
        <span className="mono tool-name">{card.call.name}</span>
        <span className={`badge ${status === 'error' ? 'bad' : status === 'waiting' || status === 'stopped' ? 'warn' : ''}`}>{LABEL[status]}</span>
        {durationMs !== undefined && <span className="muted mono">{formatDuration(durationMs)}</span>}
      </div>
      {members.length > 0
        ? <><Swarm members={members} /><details><summary>The briefs as sent</summary><pre className="tool-args">{args}</pre></details></>
        : <pre className="tool-args">{args}</pre>}
      {card.result && (
        <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
          <summary>{card.result.is_error ? 'Error' : members.length ? 'Result as the model got it' : 'Result'} ({card.result.content.length.toLocaleString()} characters)</summary>
          <pre className={`tool-result ${card.result.is_error ? 'bad' : ''}`}>{visible(card.result.content)}</pre>
        </details>
      )}
    </div>
  )
}
