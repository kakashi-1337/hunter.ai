import { useState } from 'react'
import { uploadUrl } from '../api'
import { useStore } from '../store'
import { branchLabel, callDurations, cardStatus, messageText, renderItems, siblingPosition } from '../tree'
import type { AnswerCheckBlock, Block, Message } from '../types'
import { copyText, Markdown } from './Markdown'
import { SourcesList } from './SourcesList'
import { answerCheckLine, answerCheckOf, sourcesOf, visible } from '../tree'
import { confirmBox } from '../dialog'
import { ToolCard } from './ToolCard'

const STATUS_NOTE: Partial<Record<Message['status'], string>> = {
  error: 'Failed',
  interrupted: 'Interrupted (the app stopped mid-answer)',
  cancelled: 'Stopped',
}

function Attachments({ blocks }: { blocks: Block[] }) {
  const items = blocks.filter((b) => b.type === 'image' || b.type === 'file')
  if (!items.length) return null
  return (
    <div className="attachments">
      {items.map((b) =>
        b.type === 'image' ? (
          <a key={b.upload_id} href={uploadUrl(b.upload_id)} target="_blank" rel="noopener noreferrer">
            <img className="thumb" src={uploadUrl(b.upload_id)} alt="attached image" loading="lazy" />
          </a>
        ) : b.type === 'file' ? (
          <span key={b.upload_id} className="chip" title={b.text === null ? 'Binary file: the model only sees its name and size' : undefined}>
            <a href={uploadUrl(b.upload_id)}>📎 {b.filename}</a>
            {b.truncated && <span className="badge warn" title="Only the first 200,000 characters are sent to the model">truncated</span>}
            {b.text === null && <span className="badge">binary</span>}
          </span>
        ) : null,
      )}
    </div>
  )
}

/** What a second model found when it held this answer against the run's tool results. A reading, not proof. */
function AnswerCheck({ block }: { block: AnswerCheckBlock }) {
  const { text, level } = answerCheckLine(block)
  if (!block.checked) return null                      // the answer claimed nothing about the run: nothing to show
  return (
    <div className={`answer-check ${level}`} role={block.claims.length ? 'alert' : 'note'}>
      <div>{text}</div>
      {block.claims.map((c, i) => (
        <div key={i} className="answer-claim">
          <span className={`badge ${c.verdict === 'contradicted' ? 'bad' : 'warn'}`}>{c.verdict === 'contradicted' ? 'contradicted' : 'not shown'}</span>
          <span>{visible(c.claim)}</span>
          {c.evidence && <div className="hint">{c.verdict === 'contradicted' ? 'The tool result says: ' : 'Why: '}<span className="mono">{visible(c.evidence)}</span></div>}
        </div>
      ))}
      <div className="hint">Read by {visible(block.model)}, not proof either way. A statement backed by an earlier run of this chat is not in front of it.</div>
    </div>
  )
}

export function MessageItem({ message }: { message: Message }) {
  const busy = useStore((s) => s.live !== null)
  const { edit, retry, remove, switchBranch } = useStore.getState()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [copied, setCopied] = useState(false)
  const text = messageText(message)
  const asks = useStore((s) => s.asks)
  const running = useStore((s) => s.runningCalls)
  const steps = useStore((s) => s.live?.steps ?? s.runDetail?.steps)
  const durations = callDurations(steps ?? [])
  const items = message.role === 'assistant' ? renderItems(message) : []
  const streaming = message.status === 'streaming'
  const { index, count } = siblingPosition(message)
  const isUser = message.role === 'user'

  const save = async () => {
    setEditing(false)
    if (draft.trim() && draft !== text) await edit(message.id, draft)
  }
  const copy = async () => {
    setCopied(await copyText(text))
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <article className={`msg ${message.role}`} data-status={message.status}>
      <div className="who">
        <span className="role">{isUser ? 'You' : 'Assistant'}</span>
        {!isUser && message.model && <span className="mono">{message.provider}/{message.model}</span>}
        {message.fallback_from && (
          <span className="badge warn" title={`${message.fallback_from} did not answer`}>
            answered by fallback: {message.provider}/{message.model}
          </span>
        )}
        {STATUS_NOTE[message.status] && <span className="badge bad">{STATUS_NOTE[message.status]}</span>}
        {count > 1 && (
          <span className="row" aria-label="Versions of this message">
            <button className="icon" disabled={busy || index === 0} aria-label="Previous version" onClick={() => switchBranch(message.siblings[index - 1])}>‹</button>
            <span className="mono">{branchLabel(message)}</span>
            <button className="icon" disabled={busy || index === count - 1} aria-label="Next version" onClick={() => switchBranch(message.siblings[index + 1])}>›</button>
          </span>
        )}
        <span className="tools">
          {isUser && <button className="icon" disabled={busy} onClick={() => { setDraft(text); setEditing(true) }}>Edit</button>}
          {!isUser && <button className="icon" disabled={busy} onClick={() => retry(message.id)}>Retry</button>}
          {text && <button className="icon" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>}
          <button className="icon" disabled={busy}
            onClick={() => void confirmBox('Delete this message and everything after it on this branch?').then((ok) => { if (ok) void remove(message.id) })}>Delete</button>
        </span>
      </div>

      {editing ? (
        <div className="edit-box">
          <textarea autoFocus value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') setEditing(false); if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void save() }} />
          <div className="row">
            <span className="hint grow">Saving resends from here as a new version. The old one is kept.</span>
            <button className="btn" onClick={() => setEditing(false)}>Cancel</button>
            <button className="btn primary" disabled={!draft.trim()} onClick={save}>Save and resend</button>
          </div>
        </div>
      ) : isUser ? (
        <>
          {text && <div className="bubble">{text}</div>}
          <Attachments blocks={message.blocks} />
        </>
      ) : (
        <>
          {/* In the order things happened. Only the model's own text goes to Markdown. */}
          {items.map((item, i) =>
            item.kind === 'thinking' ? (
              <details key={i} className="thinking">
                <summary>Thinking{streaming && i === items.length - 1 ? '…' : ''}</summary>
                <pre>{item.text}</pre>
              </details>
            ) : item.kind === 'text' ? (
              <Markdown key={i} text={item.text} />
            ) : item.kind === 'note' ? (
              <div key={i} className="owner-note"><div className="k">You, while the agent worked (read here)</div><div className="bubble">{item.text}</div></div>
            ) : item.kind === 'notice' ? (
              <div key={i} className={`answer-notice ${item.level}`} role="note">{item.text}</div>
            ) : (
              <ToolCard key={item.card.call.id} card={item.card} status={cardStatus(item.card, asks, running, !streaming)}
                durationMs={durations[item.card.call.id]} steps={item.card.call.name === 'swarm' ? steps : undefined} />
            ))}
          {items.length === 0 && (streaming ? <span className="muted cursor">Waiting for the model</span> : <span className="muted">No output.</span>)}
          {streaming && items.length > 0 && items[items.length - 1].kind === 'text' && <span className="cursor" />}
          {sourcesOf(message) && <SourcesList block={sourcesOf(message)!} answer={text} />}
          {answerCheckOf(message) && <AnswerCheck block={answerCheckOf(message)!} />}
        </>
      )}
    </article>
  )
}
