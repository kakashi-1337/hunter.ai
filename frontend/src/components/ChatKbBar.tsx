import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import { EVERY_CHAT_TITLE, FOLLOWS_TITLE, noKnowledgeLine } from '../kb'
import { useStore } from '../store'
import type { ChatKb, Collection } from '../types'

/** Which knowledge collections this chat reads, and whether every message pulls from them. Beside each collection:
 * whether every chat reads it that has none of its own, which is the owner's setting and not this chat's. */
export function ChatKbBar() {
  const chatId = useStore((s) => s.chat?.id)
  const busy = useStore((s) => s.live !== null)
  const setError = useStore((s) => s.setError)
  const [kb, setKb] = useState<ChatKb | null>(null)
  const [collections, setCollections] = useState<Collection[]>([])
  const [every, setEvery] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const current = useRef(chatId)
  current.current = chatId

  useEffect(() => {
    setKb(null); setOpen(false)
    if (!chatId) return
    let stop = false
    Promise.all([api.getChatKb(chatId), api.listCollections()])
      .then(([k, c]) => { if (!stop) { setKb(k); setCollections(c) } })
      .catch(() => {})
    api.getNewChatKb().then((n) => { if (!stop) setEvery(n.collection_ids) }).catch(() => {})     // a server from before it had them
    return () => { stop = true }
  }, [chatId])

  if (!chatId || !kb) return null
  const save = async (patch: Parameters<typeof api.putChatKb>[1]) => {
    try {
      const next = await api.putChatKb(chatId, patch)
      if (current.current === chatId) setKb(next)     // an answer for a chat that is no longer open is dropped
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const toggle = (id: string) =>
    save({ collection_ids: kb.collection_ids.includes(id) ? kb.collection_ids.filter((c) => c !== id) : [...kb.collection_ids, id] })
  const toggleEvery = async (id: string) => {
    try {
      setEvery((await api.putNewChatKb(every.includes(id) ? every.filter((c) => c !== id) : [...every, id])).collection_ids)
      const next = await api.getChatKb(chatId)        // a chat that follows them reads something else now
      if (current.current === chatId) setKb(next)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const openMenu = async () => {
    if (!open) {
      setCollections(await api.listCollections().catch(() => collections))
      setEvery(await api.getNewChatKb().then((n) => n.collection_ids).catch(() => every))
    }
    setOpen(!open)
  }
  const attached = collections.filter((c) => kb.collection_ids.includes(c.id))

  return (
    <div className="kb-bar" aria-label="Knowledge for this chat">
      {attached.map((c) => (
        <span key={c.id} className="chip" title={kb.follows ? FOLLOWS_TITLE : undefined}>{c.name}{kb.follows && <span className="muted"> · every chat</span>}
          <button className="icon" aria-label={`Detach ${c.name}`} disabled={busy} onClick={() => toggle(c.id)}>✕</button>
        </span>
      ))}
      <span className="kb-menu">
        <button className="btn small" disabled={busy} aria-expanded={open} onClick={() => void openMenu()}>+ Knowledge</button>
        {open && (
          <div className="kb-pop">
            {collections.length === 0 && <div className="hint">No collection yet. Create one under Knowledge in the sidebar.</div>}
            {collections.map((c) => (
              <div key={c.id} className="kb-row">
                <label className="check"><input type="checkbox" checked={kb.collection_ids.includes(c.id)} disabled={busy} onChange={() => toggle(c.id)} /><span>{c.name}</span></label>
                <label className="check every" title={EVERY_CHAT_TITLE}>
                  <input type="checkbox" aria-label={`Attach ${c.name} to every chat`} checked={every.includes(c.id)} onChange={() => void toggleEvery(c.id)} /><span>every chat</span>
                </label>
              </div>
            ))}
          </div>
        )}
      </span>
      <label className="check" title={kb.collection_ids.length ? 'Every message first searches the collections this chat reads; the best passages go to the model as untrusted reference. Set here, it is this chat\'s own choice; Settings has the switch for every chat that did not choose.' : 'Attach a collection first'}>
        <input type="checkbox" checked={kb.always_retrieve} disabled={busy || kb.collection_ids.length === 0}
          onChange={(e) => save({ always_retrieve: e.target.checked })} />
        <span>Always retrieve</span>
      </label>
      {kb.collection_ids.length === 0 && (noKnowledgeLine(0, collections.length)
        ? <span className="warn-line" role="status">{noKnowledgeLine(0, collections.length)}</span>
        : <span className="hint">Attach a collection first</span>)}
    </div>
  )
}
