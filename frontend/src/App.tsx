import { useEffect, useState } from 'react'
import { AdvancedOptions } from './components/AdvancedOptions'
import { ChatView } from './components/ChatView'
import { DialogHost } from './components/DialogHost'
import { GoalPanel } from './components/GoalPanel'
import { KnowledgeView } from './components/KnowledgeView'
import { ModelPicker } from './components/ModelPicker'
import { PermissionsPanel } from './components/PermissionsPanel'
import { ProcessPanel } from './components/ProcessPanel'
import { SettingsDialog } from './components/SettingsDialog'
import { SetupChecklist } from './components/SetupChecklist'
import { Sidebar } from './components/Sidebar'
import { TrajectoryPanel } from './components/TrajectoryPanel'
import * as api from './api'
import { confirmBox } from './dialog'
import { useChatView, useStore } from './store'
import { AUTO_APPROVE_WARNING, withAuto } from './tree'
import type { Mode } from './types'

const MODES: Mode[] = ['plan', 'pentesting', 'coding']

function ChatHeader({ trajectoryOpen, onToggleTrajectory }: { trajectoryOpen: boolean; onToggleTrajectory: () => void }) {
  const chat = useChatView()
  const targets = useStore((s) => s.targets)
  const newChat = useStore((s) => s.newChat)
  const busy = useStore((s) => s.live !== null)
  const patchChat = useStore((s) => s.patchChat)
  const personas = useStore((s) => s.personas)
  const choosePersona = useStore((s) => s.choosePersona)
  const steps = useStore((s) => s.live?.steps.length ?? 0)
  const [panel, setPanel] = useState<'permissions' | 'processes' | 'goal' | null>(null)
  const [running, setRunning] = useState(0)
  const chatId = chat?.id || undefined      // the stand-in for "no chat yet" has an empty id
  /** Permissions and Processes belong to a chat: open one first when there is none yet. */
  const openPanel = async (which: 'permissions' | 'processes' | 'goal') => {
    if (!useStore.getState().activeChatId) await newChat()
    setPanel(which)
  }
  const auto = chat?.permissions?.auto === true
  /** Auto-approve is a chat's own switch. Switching it on shows what it means first; off needs no question. */
  const toggleAuto = async () => {
    if (!auto && !(await confirmBox(AUTO_APPROVE_WARNING, true))) return
    if (!useStore.getState().activeChatId) await newChat()
    await patchChat({ permissions: withAuto(useStore.getState().chat?.permissions, !auto) })
  }
  // How many processes of this chat are running: refreshed as the run makes progress.
  useEffect(() => {
    if (!chatId) return
    let stop = false
    api.listProcesses(chatId).then((list) => { if (!stop) setRunning(list.filter((p) => p.status === 'running').length) }).catch(() => {})
    return () => { stop = true }
  }, [chatId, steps, busy, panel])
  if (!chat) return null
  return (
    <header className="chat-head">
      <span className="title grow" title={chat.title}>{chat.title}</span>
      <select aria-label="Target" value={chat.target_id ?? ''} onChange={(e) => patchChat({ target_id: e.target.value || null })}>
        <option value="">No target</option>
        {targets.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
      </select>
      <select aria-label="Persona" title="Who the assistant is for this chat" value={chat.persona_id ?? ''} disabled={busy} onChange={(e) => void choosePersona(e.target.value || null)}>
        <option value="">No persona</option>
        {personas.map((p) => <option key={p.id} value={p.id} title={p.description}>{p.name}</option>)}
      </select>
      <div className="seg" role="group" aria-label="Mode">
        {MODES.map((m) => (
          <button key={m} className={chat.mode === m ? 'on' : ''} aria-pressed={chat.mode === m} disabled={busy} onClick={() => patchChat({ mode: m })}>{m}</button>
        ))}
      </div>
      <ModelPicker />
      <AdvancedOptions />
      <button className="btn" title="Goal record and findings" onClick={() => void openPanel('goal')}>Goal</button>
      <button className="btn" onClick={() => void openPanel('permissions')}>Permissions</button>
      <button className={`btn${auto ? ' danger-solid' : ''}`} aria-pressed={auto} disabled={busy} onClick={() => void toggleAuto()}
        title={auto ? 'Auto-approve is on for this chat: calls run without asking, except where one touches what always asks. Click to switch it off.' : 'Stop asking before each call in this chat (you are shown what that means first)'}>
        {auto ? 'Auto-approve: on' : 'Auto-approve'}</button>
      <button className="btn" onClick={() => void openPanel('processes')}>Processes{running ? ` (${running})` : ''}</button>
      <button className="btn" aria-pressed={trajectoryOpen} title="Toggle trajectory (Ctrl+J)" onClick={onToggleTrajectory}>Trajectory</button>
      {panel === 'permissions' && <PermissionsPanel onClose={() => setPanel(null)} />}
      {panel === 'goal' && <GoalPanel onClose={() => setPanel(null)} />}
      {panel === 'processes' && <ProcessPanel onClose={() => setPanel(null)} />}
    </header>
  )
}

export default function App() {
  const load = useStore((s) => s.load)
  const error = useStore((s) => s.error)
  const setError = useStore((s) => s.setError)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const view = useStore((s) => s.view)
  const setView = useStore((s) => s.setView)
  const [trajectoryOpen, setTrajectoryOpen] = useState(() => {
    try { return localStorage.getItem('trajectory') !== 'closed' } catch { return true }
  })
  const [setupOpen, setSetupOpen] = useState(false)
  useEffect(() => { void load() }, [load])
  // A first start with no model to talk to opens the setup check by itself; later it is under Settings.
  useEffect(() => { api.systemCheckup().then((c) => { if (!c.ready) setSetupOpen(true) }).catch(() => {}) }, [])
  useEffect(() => {
    try { localStorage.setItem('trajectory', trajectoryOpen ? 'open' : 'closed') } catch { /* private mode */ }
  }, [trajectoryOpen])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j') { e.preventDefault(); setTrajectoryOpen((o) => !o) }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'k') { e.preventDefault(); const s = useStore.getState(); s.setView(s.view === 'knowledge' ? 'chat' : 'knowledge') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="app">
      <Sidebar onSettings={() => setSettingsOpen(true)} />
      {view === 'knowledge' ? <KnowledgeView onClose={() => setView('chat')} /> : <ChatView header={
        <>
          <ChatHeader trajectoryOpen={trajectoryOpen} onToggleTrajectory={() => setTrajectoryOpen((o) => !o)} />
          {error && (
            <div className="banner" role="alert">
              <span className="grow">{error}</span>
              <button className="icon" aria-label="Dismiss" onClick={() => setError(null)}>✕</button>
            </div>
          )}
        </>
      } />}
      {trajectoryOpen && view === 'chat' && <TrajectoryPanel onClose={() => setTrajectoryOpen(false)} />}
      <DialogHost />
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} onSetup={() => { setSettingsOpen(false); setSetupOpen(true) }} />}
      {setupOpen && <SetupChecklist onClose={() => setSetupOpen(false)} onSettings={() => { setSetupOpen(false); setSettingsOpen(true) }} />}
    </div>
  )
}
