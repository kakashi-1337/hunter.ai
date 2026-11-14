import { useEffect, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import { visible } from '../tree'
import type { Memory, MemorySettings } from '../types'
import { Modal } from './Modal'

const text = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** What HunterAI remembers across chats: read it, correct it, remove it, add to it. */
export function MemoryManager({ onClose, onLeave }: { onClose: () => void; onLeave?: () => void }) {
  const { targets, chats, chat } = useStore()
  const openChat = useStore((s) => s.openChat)
  const [items, setItems] = useState<Memory[]>([])
  const [settings, setSettings] = useState<MemorySettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [fresh, setFresh] = useState('')
  const [kind, setKind] = useState<Memory['kind']>('fact')
  const reload = async () => {
    try { const r = await api.listMemories(); setItems(r.items); setSettings(r.settings) } catch (e) { setError(text(e)) }
  }
  useEffect(() => { void reload() }, [])
  const run = async (fn: () => Promise<unknown>) => { setError(null); setNote(null); try { await fn(); await reload() } catch (e) { setError(text(e)) } }
  const targetName = (id: string | null) => (id ? targets.find((t) => t.id === id)?.name ?? 'a deleted target' : null)

  return (
    <Modal title="Memory" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
      <div className="hint">Notes a model took from your chats after they went quiet (about {Math.round((settings?.idle_seconds ?? 600) / 60)} minutes), each linked to the chat it came from. They are offered to later chats as background, not as instructions. A note about a target is shown only in chats on that target; a note from a chat without a target is shown only in chats without one, except your own preferences, which apply everywhere. A note can be wrong: correct or delete it here. Text that looks like a password, key, or token is refused; that filter catches common shapes, not every secret, so do not rely on it.</div>
      {settings && (<>
        <label className="check"><input type="checkbox" checked={settings.enabled} onChange={(e) => void run(() => api.putMemorySettings({ enabled: e.target.checked }))} /><span>Take notes from chats that have gone quiet (one decision-model call per chat)</span></label>
        <label className="check"><input type="checkbox" checked={settings.recall} onChange={(e) => void run(() => api.putMemorySettings({ recall: e.target.checked }))} /><span>Offer matching notes to new messages</span></label>
      </>)}
      {chat && <div className="row"><button className="btn small" onClick={() => void run(async () => { await api.memorizeChat(chat.id); setNote('Queued. The notes appear here once the worker has read the chat.') })}>Take notes from the open chat now</button>{note && <span className="hint">{note}</span>}</div>}
      {error && <div className="ask-warning" role="alert">{error}</div>}
      <div className="row">
        <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value as Memory['kind'])}><option value="fact">fact</option><option value="lesson">lesson</option><option value="preference">preference</option></select>
        <input className="grow" placeholder="Add a note by hand (one or two sentences)" value={fresh} maxLength={500} onChange={(e) => setFresh(e.target.value)} />
        <button className="btn" disabled={!fresh.trim()} onClick={() => void run(async () => { await api.addMemory(fresh, kind, chat?.target_id); setFresh('') })}>Add</button>
      </div>
      <h3 className="section">Notes ({items.length})</h3>
      {items.length === 0 && <div className="hint">Nothing remembered yet.</div>}
      {items.map((m) => (
        <div key={m.id} className="mcp-server">
          <div className="row"><span className="badge">{m.kind}</span>
            {targetName(m.target_id) && <span className="muted">{targetName(m.target_id)}</span>}
            {m.chat_id && chats.some((c) => c.id === m.chat_id)
              ? <button className="kb-link muted" title="Open the chat this came from" onClick={() => { void openChat(m.chat_id); onClose(); onLeave?.() }}>from “{chats.find((c) => c.id === m.chat_id)?.title}”</button>
              : <span className="muted">{m.chat_id ? 'from a deleted chat' : 'added by hand'}</span>}
            <span className="grow" />
            {editing === m.id ? (<>
              <button className="btn small" onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn small primary" disabled={!draft.trim()} onClick={() => void run(async () => { await api.changeMemory(m.id, draft); setEditing(null) })}>Save</button></>
            ) : (<>
              <button className="btn small" onClick={() => { setEditing(m.id); setDraft(m.text) }}>Edit</button>
              <button className="btn small" onClick={() => { if (window.confirm('Forget this note?')) void run(() => api.deleteMemory(m.id)) }}>Delete</button></>)}
          </div>
          {editing === m.id ? <textarea rows={3} value={draft} maxLength={500} onChange={(e) => setDraft(e.target.value)} /> : <div>{visible(m.text)}</div>}
        </div>
      ))}
    </Modal>
  )
}
