import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import type { Chat, Target } from '../types'
import { Modal } from './Modal'
import { ActivityPanel } from './ActivityPanel'
import { CaidoPanel } from './CaidoPanel'
import { FindingsPanel } from './FindingsPanel'
import { confirmBox } from '../dialog'
import { MachineStatus } from './MachineStatus'

const lines = (s: string) => s.split('\n').map((x) => x.trim()).filter(Boolean)

function TargetDialog({ target, onClose }: { target: Target | null; onClose: () => void }) {
  const refreshLists = useStore((s) => s.refreshLists)
  const setError = useStore((s) => s.setError)
  const [name, setName] = useState(target?.name ?? '')
  const [notes, setNotes] = useState(target?.notes ?? '')
  const [inScope, setInScope] = useState((target?.scope.in ?? []).join('\n'))
  const [outScope, setOutScope] = useState((target?.scope.out ?? []).join('\n'))
  const save = async () => {
    try {
      const scope = { in: lines(inScope), out: lines(outScope) }
      if (target) await api.updateTarget(target.id, { name: name.trim(), notes, scope })
      else {
        const t = await api.createTarget(name.trim())
        await api.updateTarget(t.id, { notes, scope })
      }
      await refreshLists()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <Modal
      title={target ? 'Edit target' : 'New target'}
      onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!name.trim()} onClick={save}>Save</button>
      </>}
    >
      <label className="field"><span>Name</span>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Program or client name" />
      </label>
      <label className="field"><span>Notes</span>
        <textarea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Rules of engagement, accounts, anything the model should know" />
      </label>
      <label className="field"><span>In scope (one per line)</span>
        <textarea className="mono" rows={4} value={inScope} onChange={(e) => setInScope(e.target.value)} placeholder={'*.example.com\napi.example.com/v2'} />
      </label>
      <label className="field"><span>Out of scope (one per line)</span>
        <textarea className="mono" rows={3} value={outScope} onChange={(e) => setOutScope(e.target.value)} placeholder="payments.example.com" />
      </label>
      <div className="hint">Scope is given to the model as context and checked on every tool call: a host on the out-of-scope list, or one that is not on the in-scope list, always asks you first.</div>
    </Modal>
  )
}

function ChatRow({ chat }: { chat: Chat }) {
  const active = useStore((s) => s.activeChatId === chat.id)
  const openChat = useStore((s) => s.openChat)
  const removeChat = useStore((s) => s.removeChat)
  const refreshLists = useStore((s) => s.refreshLists)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(chat.title)
  const commit = async () => {
    setRenaming(false)
    const t = title.trim()
    if (t && t !== chat.title) {
      await api.updateChat(chat.id, { title: t })
      await refreshLists()
      if (active) await openChat(chat.id)
    } else setTitle(chat.title)
  }
  if (renaming) {
    return (
      <div className="chat-row">
        <input autoFocus value={title} onChange={(e) => setTitle(e.target.value)} onBlur={commit}
          onKeyDown={(e) => { if (e.key === 'Enter') void commit(); if (e.key === 'Escape') { setTitle(chat.title); setRenaming(false) } }} />
      </div>
    )
  }
  return (
    <div className={`chat-row${active ? ' active' : ''}`}>
      <button className="open" title={chat.title} onClick={() => openChat(chat.id)} onDoubleClick={() => setRenaming(true)}>{chat.title}</button>
      <span className="actions">
        <button className="icon" title="Rename" aria-label={`Rename ${chat.title}`} onClick={() => { setTitle(chat.title); setRenaming(true) }}>✎</button>
        <button className="icon" title="Delete chat" aria-label={`Delete ${chat.title}`}
          onClick={() => void confirmBox(`Delete "${chat.title}" and all its messages?`).then((ok) => { if (ok) void removeChat(chat.id) })}>✕</button>
      </span>
    </div>
  )
}

export function Sidebar({ onSettings }: { onSettings: () => void }) {
  const [panel, setPanel] = useState<'findings' | 'caido' | 'activity' | null>(null)
  const { targets, chats, searchResults } = useStore()
  const newChat = useStore((s) => s.newChat)
  const view = useStore((s) => s.view)
  const setView = useStore((s) => s.setView)
  const openChat = useStore((s) => s.openChat)
  const runSearch = useStore((s) => s.runSearch)
  const refreshLists = useStore((s) => s.refreshLists)
  const [query, setQuery] = useState('')
  const [dialog, setDialog] = useState<{ target: Target | null } | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const t = setTimeout(() => void runSearch(query), 180)
    return () => clearTimeout(t)
  }, [query, runSearch])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey
      if (mod && !e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); searchRef.current?.focus(); searchRef.current?.select() }
      if (mod && e.shiftKey && e.key.toLowerCase() === 'o') { e.preventDefault(); void newChat(null) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [newChat])

  const removeTarget = async (t: Target) => {
    if (!(await confirmBox(`Delete target "${t.name}"? Its chats are kept and move to "No target".`))) return
    await api.deleteTarget(t.id)
    await refreshLists()
  }
  const groups: { target: Target | null; chats: Chat[] }[] = [
    ...targets.map((t) => ({ target: t, chats: chats.filter((c) => c.target_id === t.id) })),
    { target: null, chats: chats.filter((c) => !c.target_id || !targets.some((t) => t.id === c.target_id)) },
  ]

  return (
    <aside className="sidebar">
      <div className="side-head">
        <div className="row">
          <span className="brand grow">Hunter<b>AI</b></span>
          <button className="btn" title="New target" onClick={() => setDialog({ target: null })}>+ Target</button>
          <button className="btn primary" title="New chat (Ctrl+Shift+O)" onClick={() => newChat(null)}>+ Chat</button>
        </div>
        <input ref={searchRef} type="search" placeholder="Search messages (Ctrl+K)" value={query} aria-label="Search messages"
          onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === 'Escape' && setQuery('')} />
      </div>
      <nav className="side-list">
        {searchResults ? (
          searchResults.length ? searchResults.map((h) => (
            <button key={h.message_id} className="hit" onClick={() => { void openChat(h.chat_id) }}>
              <div className="t">{h.chat_title}</div>
              <div className="s">{h.snippet}</div>
            </button>
          )) : <div className="empty">No messages match “{query}”.</div>
        ) : groups.map(({ target, chats: list }) => (
          <section key={target?.id ?? 'none'}>
            <div className="group-head">
              <span className="name" title={target?.name}>{target ? target.name : 'No target'}</span>
              <span className="actions">
                <button className="icon" title="New chat here" aria-label={`New chat in ${target?.name ?? 'No target'}`} onClick={() => newChat(target?.id ?? null)}>＋</button>
                {target && <button className="icon" title="Edit target and scope" aria-label={`Edit ${target.name}`} onClick={() => setDialog({ target })}>✎</button>}
                {target && <button className="icon" title="Delete target" aria-label={`Delete ${target.name}`} onClick={() => removeTarget(target)}>✕</button>}
              </span>
            </div>
            {list.length ? list.map((c) => <ChatRow key={c.id} chat={c} />) : <div className="empty">No chats yet</div>}
          </section>
        ))}
      </nav>
      <div className="side-foot">
        <MachineStatus />
        <div className="side-buttons">
          <button className="btn" title="Every finding of every target" onClick={() => setPanel('findings')}>Findings</button>
          <button className="btn" title="What Caido recorded of the agent's requests" onClick={() => setPanel('caido')}>Caido</button>
          <button className="btn" title="Calls, tokens and failures, counted on this machine" onClick={() => setPanel('activity')}>Activity</button>
          <button className="btn" title="Knowledge base (Ctrl+Shift+K)" onClick={() => setView(view === 'knowledge' ? 'chat' : 'knowledge')}>Knowledge</button>
          <button className="btn" onClick={onSettings}>Settings</button>
        </div>
      </div>
      {panel === 'findings' && <FindingsPanel onClose={() => setPanel(null)} />}
      {panel === 'caido' && <CaidoPanel onClose={() => setPanel(null)} />}
      {panel === 'activity' && <ActivityPanel onClose={() => setPanel(null)} />}
      {dialog && <TargetDialog target={dialog.target} onClose={() => setDialog(null)} />}
    </aside>
  )
}
