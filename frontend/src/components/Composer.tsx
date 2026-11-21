import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import { GO_TEXT, slashCommand, slashMenu, type SlashContext, type SlashItem, type SlashResult } from '../slash'
import { useChatView, useStore } from '../store'
import { pruneOptions } from '../tree'
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
  // Slash commands: a line that starts with "/" and a known word changes the chat instead of being sent.
  const chat = useChatView()
  const targets = useStore((s) => s.targets)
  const personas = useStore((s) => s.personas)
  const providers = useStore((s) => s.providers)
  const skills = useStore((s) => s.skills)
  const [row, setRow] = useState(0)                                   // the highlighted row of the menu
  const [closedFor, setClosedFor] = useState<string | null>(null)     // Escape closes the menu for the text as it stands
  const [said, setSaid] = useState('')                                // what the last command did, or why it did not
  const slash: SlashContext = { busy, provider: chat?.provider ?? '', model: chat?.model ?? '', targets, personas, providers, skills: skills.filter((s) => s.enabled) }
  const menu = text === closedFor ? null : slashMenu(text, slash)
  const at = menu?.length ? Math.min(row, menu.length - 1) : 0

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
        if (up.problem) setError(`${up.filename}: ${up.problem}`)       // told now, not by the model three messages later
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
  /** Carries out a command. Null when that was all (or it was refused and the text stays to be corrected);
   * a string when a message goes to the model now: the line of a skill, or the approval that /go sends. */
  const perform = async (r: SlashResult): Promise<string | null> => {
    const store = useStore.getState()
    if (r.kind === 'refused') { setSaid(r.message); return null }
    if (r.kind === 'skill' || r.kind === 'go') {
      const now = chat?.mode ?? 'pentesting'
      // A skill with a mode moves the chat there first (/plan: nothing can be changed), and /go moves it back.
      const next = r.kind === 'skill' ? r.mode ?? now : r.mode ?? store.modeBefore[chatId ?? ''] ?? (now === 'plan' ? 'pentesting' : now)
      if (next !== now) {
        if (r.kind === 'skill') store.rememberMode(chatId ?? '', now)
        await store.patchChat({ mode: next })
      }
      return r.kind === 'skill' ? r.text : GO_TEXT
    }
    setText('')
    if (r.kind === 'skills') store.setSkillsOpen(true)
    else if (r.kind === 'new') await store.newChat(chat?.target_id ?? null)
    else if (r.kind === 'stop') { await store.cancel(); setSaid('Stopping the run.') }
    else if (r.kind === 'mode') { await store.patchChat({ mode: r.mode }); setSaid(`Mode: ${r.mode}.`) }
    else if (r.kind === 'persona') { await store.choosePersona(r.id); setSaid(`Persona: ${personas.find((p) => p.id === r.id)?.name ?? 'none'}.`) }
    else if (r.kind === 'target') { await store.patchChat({ target_id: r.id }); setSaid(`Target: ${targets.find((t) => t.id === r.id)?.name ?? 'none'}.`) }
    else if (r.kind === 'model') {
      const info = providers.find((p) => p.name === r.provider)?.models.find((m) => m.id === r.model)
      // Options the new model does not accept are dropped rather than sent and rejected.
      await store.patchChat({ provider: r.provider, model: r.model, options: pruneOptions(chat?.options ?? {}, info?.options ?? []) })
      setSaid(`Model: ${r.provider}/${r.model}.`)
    } else {
      const options = { ...(chat?.options ?? {}) }
      if (r.effort) options.effort = r.effort
      else delete options.effort
      await store.patchChat({ options })
      setSaid(`Effort: ${r.effort ?? "the model's own"}.`)
    }
    return null
  }
  const submit = async (typed = text) => {
    const command = slashCommand(typed, slash)
    let outgoing = typed
    if (command) {
      if ((command.kind === 'skill' || command.kind === 'go') && uploading > 0) { setSaid('Wait for the upload to finish.'); return }
      const message = await perform(command)
      if (message === null) return
      outgoing = message
    }
    if (canQueue && !command) {
      const t = text
      setText('')
      if (!(await queueMessage(t))) setText((now) => now || t)
      return
    }
    if (!command && !canSend) return
    const t = typed, sent = files, before = chatId
    setText('')
    setFiles([])
    if (await send(outgoing, sent.map((f) => f.id))) {
      // The first message made the chat: what /go needs to know moves to the chat's own id.
      const made = useStore.getState().activeChatId
      const kept = useStore.getState().modeBefore['']
      if (!before && made && kept) useStore.getState().rememberMode(made, kept)
      return
    }
    // Not sent (the banner says why): give the prompt back unless something new was typed meanwhile.
    setText((now) => now || t)
    setFiles((now) => (now.length ? now : sent))
  }
  const edit = (value: string) => { setText(value); setRow(0); setClosedFor(null); setSaid('') }
  /** Takes a row of the menu: the text becomes the command, and a whole command runs when asked to. */
  const take = (item: SlashItem, run: boolean) => {
    edit(item.text)
    area.current?.focus()
    if (run && item.run) void submit(item.text)
  }
  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (menu?.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setRow((at + (e.key === 'ArrowDown' ? 1 : menu.length - 1)) % menu.length); return }
      if (e.key === 'Tab') { e.preventDefault(); take(menu[at], false); return }
      if (e.key === 'Escape') { e.preventDefault(); setClosedFor(text); return }
    }
    if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing) return
    e.preventDefault()
    // Enter on a command that is not whole yet takes the highlighted row; on a whole one it runs it.
    const whole = slashCommand(text, slash)
    if (menu?.length && (!whole || whole.kind === 'refused')) take(menu[at], true)
    else void submit()
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
                <span key={f.id} className="chip" title={f.problem || undefined}>
                  {f.problem ? '⚠' : f.mime.startsWith('image/') ? '🖼' : '📎'} {f.filename}
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
          {menu && (
            <div className="slash" role="listbox" aria-label="Commands">
              {menu.length === 0 && <div className="hint">Nothing matches. Escape closes this list.</div>}
              {menu.map((item, i) => (
                <button key={item.text} role="option" aria-selected={i === at} className={i === at ? 'on' : ''}
                  ref={i === at ? (el) => el?.scrollIntoView({ block: 'nearest' }) : undefined}
                  onMouseDown={(e) => { e.preventDefault(); take(item, true) }}>
                  <span className="mono">{item.label}</span><span className="hint">{item.hint}</span>
                </button>
              ))}
            </div>
          )}
          <textarea ref={area} rows={1} value={text} aria-label="Message"
            placeholder={busy ? 'The agent is working — Enter keeps your message for its next step, without stopping it' : 'Message HunterAI — Enter to send, Shift+Enter for a new line, / for commands'}
            onChange={(e) => edit(e.target.value)}
            onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); void add(e.clipboardData.files) } }}
            onKeyDown={onKey} />
          <div className="bar">
            <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) void add(e.target.files); e.target.value = '' }} />
            <button className="btn" onClick={() => picker.current?.click()}>Attach</button>
            <span className="note grow" role="status">{said || 'Drop or paste files. Up to 50 MB each. Type / for commands.'}</span>
            {busy
              ? <><button className="btn primary" disabled={!canQueue} title="Keep this message for the agent's next step; it is not interrupted" onClick={() => void submit()}>Queue</button>
                <button className="btn danger" title="Stop the run now" onClick={() => cancel()}>Stop</button></>
              : <button className="btn primary" disabled={!canSend} onClick={() => void submit()}>Send</button>}
          </div>
        </div>
      </div>
    </div>
  )
}
