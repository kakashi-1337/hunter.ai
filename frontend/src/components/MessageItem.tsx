import { useState } from 'react'
import { uploadUrl } from '../api'
import { useStore } from '../store'
import { branchLabel, callDurations, cardStatus, messageText, renderItems, siblingPosition } from '../tree'
import type { AnswerCheckBlock, Block, Message } from '../types'
import { copyText, Markdown } from './Markdown'
import { SourcesList } from './SourcesList'
import { answerCheckLine, answerCheckOf, changesOf, runningCheck, sourcesOf, visible, waitingToCool } from '../tree'
import { RunChanges } from './RunChanges'
import { confirmBox } from '../dialog'
import { ToolCard } from './ToolCard'

const STATUS_NOTE: Partial<Record<Message['status'], string>> = {
  error: 'Failed',
  interrupted: 'Interrupted (the app stopped mid-answer)',
  cancelled: 'Stopped',
}

const NOT_SHOWN = 'The browser cannot show this file as a picture: most likely it is damaged or cut short (or the file is no longer on this machine). A model is not sent a picture that cannot be decoded: it is told that one was left out, and why.'

/** A picture of a message. One the browser cannot show is said to be that: the words "attached image" alone,
 * which is what a browser puts in its place, read like a link that was never a picture. */
function Thumb({ id }: { id: string }) {
  const [broken, setBroken] = useState(false)
  if (broken) return <span className="chip" title={NOT_SHOWN}><a href={uploadUrl(id)} target="_blank" rel="noopener noreferrer">⚠ A picture that cannot be shown</a></span>
  return (
    <a href={uploadUrl(id)} target="_blank" rel="noopener noreferrer">
      <img className="thumb" src={uploadUrl(id)} alt="attached image" loading="lazy" onError={() => setBroken(true)} />
    </a>
  )
}

export function Attachments({ blocks }: { blocks: Block[] }) {
  const items = blocks.filter((b) => b.type === 'image' || b.type === 'file')
  if (!items.length) return null
  return (
    <div className="attachments">
      {items.map((b) =>
        b.type === 'image' ? (
          <Thumb key={b.upload_id} id={b.upload_id} />
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

/** What the owner sent while the agent worked, at the place where the agent read it. It can be copied, and sent
 * once more: it waits for the agent's next step while a run is going, and is a message of its own otherwise. It
 * is part of the answer it stands in, so it goes when that answer is deleted. */
export function OwnerNote({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const [sent, setSent] = useState(false)
  const copy = async () => {
    setCopied(await copyText(text))
    setTimeout(() => setCopied(false), 1200)
  }
  const again = async () => {
    setSent(await useStore.getState().queueMessage(text))
    setTimeout(() => setSent(false), 1200)
  }
  return (
    <div className="owner-note">
      <div className="k">
        <span>You, while the agent worked (read here)</span>
        <span className="tools">
          <button className="icon" title="Send this message to the agent once more" onClick={() => void again()}>{sent ? 'Sent' : 'Send again'}</button>
          <button className="icon" onClick={() => void copy()}>{copied ? 'Copied' : 'Copy'}</button>
        </span>
      </div>
      <div className="bubble">{text}</div>
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
  // The check of the folder runs inside the run, before its first change and after its answer: say so while it does.
  const liveSteps = useStore((s) => (s.live && s.live.target === message.id ? s.live.steps : null))
  const checking = streaming && liveSteps ? runningCheck(liveSteps) : null
  // A local model is called once the machine is cool enough for it: the wait can take minutes, and nothing else shows it.
  const cooling = streaming && liveSteps ? waitingToCool(liveSteps) : null
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
              <OwnerNote key={i} text={item.text} />
            ) : item.kind === 'notice' ? (
              <div key={i} className={`answer-notice ${item.level}`} role="note">{item.text}</div>
            ) : (
              <ToolCard key={item.card.call.id} card={item.card} status={cardStatus(item.card, asks, running, !streaming)}
                durationMs={durations[item.card.call.id]} steps={item.card.call.name === 'swarm' ? steps : undefined} />
            ))}
          {items.length === 0 && (streaming ? <span className="muted cursor">Waiting for the model</span> : <span className="muted">No output.</span>)}
          {streaming && items.length > 0 && items[items.length - 1].kind === 'text' && <span className="cursor" />}
          {checking && <div className="run-changes" role="status">{visible(checking.text)}</div>}
          {cooling && <div className="run-changes warn" role="status">{cooling}</div>}
          {changesOf(message) && <RunChanges block={changesOf(message)!} />}
          {sourcesOf(message) && <SourcesList block={sourcesOf(message)!} answer={text} />}
          {answerCheckOf(message) && <AnswerCheck block={answerCheckOf(message)!} />}
        </>
      )}
    </article>
  )
}
