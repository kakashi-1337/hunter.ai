import { useState } from 'react'
import * as api from '../api'
import { emptyScopeForm, scopeOf, type ScopeForm } from '../scope'
import { useStore } from '../store'
import { Modal } from './Modal'
import { TargetForm } from './TargetForm'

const NEW = 'new'

/** One way to start a chat: with no target, with a target that exists, or with a new one made from a pasted brief.
 * It opens on the target of the chat on screen, so a chat for the same engagement is one Enter away. */
export function NewChatDialog({ onClose }: { onClose: () => void }) {
  const targets = useStore((s) => s.targets)
  const newChat = useStore((s) => s.newChat)
  const refreshLists = useStore((s) => s.refreshLists)
  const setError = useStore((s) => s.setError)
  const [choice, setChoice] = useState(() => {
    const now = useStore.getState().chat?.target_id
    return now && targets.some((t) => t.id === now) ? now : ''
  })
  const [form, setForm] = useState<ScopeForm>(emptyScopeForm)
  const [unread, setUnread] = useState(false)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState('')
  const blocked = busy || (choice === NEW && (!form.name.trim() || unread))

  const start = async () => {
    if (blocked) return
    setBusy(true)
    try {
      let targetId: string | null = choice || null
      if (choice === NEW) {
        targetId = (await api.createTarget(form.name.trim(), form.notes, scopeOf(form))).id
        await refreshLists()
      }
      await newChat(targetId)
      onClose()
    } catch (e) {
      if (e instanceof api.ApiError && e.status === 422) setProblem(e.message)     // a scope line that cannot be read: said in the form, beside the lists
      else setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  return (
    <Modal
      title="New chat"
      onClose={onClose}
      footer={<>
        {choice === NEW && unread && <span className="hint grow">Read the brief first, or clear the box.</span>}
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={blocked} onClick={() => void start()}>Start chat</button>
      </>}
    >
      <label className="field"><span>Target</span>
        <select autoFocus aria-label="Target" value={choice} onChange={(e) => setChoice(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void start() } }}>
          <option value="">No target: a normal chat</option>
          {targets.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          <option value={NEW}>New target…</option>
        </select>
      </label>
      {choice === '' && <div className="hint">No scope is checked in a chat without a target. You can still give the chat a target later, in its header.</div>}
      {choice !== '' && choice !== NEW && <div className="hint">The chat works inside this target's scope and sees its notes and findings.</div>}
      {choice === NEW && <TargetForm form={form} problem={problem} onChange={setForm} onUnread={setUnread} onEnter={() => void start()} focusName />}
    </Modal>
  )
}
