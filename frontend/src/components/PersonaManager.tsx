import { useEffect, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import type { Collection, Mode, Persona } from '../types'
import { confirmBox } from '../dialog'
import { Modal } from './Modal'

const text = (e: unknown) => (e instanceof Error ? e.message : String(e))

function Editor({ persona, onDone, onClose }: { persona: Persona | null; onDone: () => void; onClose: () => void }) {
  const providers = useStore((s) => s.providers)
  const [name, setName] = useState(persona?.name ?? '')
  const [description, setDescription] = useState(persona?.description ?? '')
  const [prompt, setPrompt] = useState(persona?.system_prompt ?? '')
  const [mode, setMode] = useState<Mode | ''>(persona?.default_mode ?? '')
  const [provider, setProvider] = useState(persona?.provider ?? '')
  const [model, setModel] = useState(persona?.model ?? '')
  const [tools, setTools] = useState<string[]>(persona?.tools ?? [])
  const [collections, setCollections] = useState<string[]>(persona?.collections ?? [])
  const [allTools, setAllTools] = useState<string[]>([])
  const [allCollections, setAllCollections] = useState<Collection[]>([])
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    api.personaTools().then(setAllTools).catch(() => {})
    api.listCollections().then(setAllCollections).catch(() => {})
  }, [])
  const models = providers.find((p) => p.name === provider)?.models ?? []
  const flip = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v])
  const save = async () => {
    const body = { name, description, system_prompt: prompt, default_mode: mode || null, provider: provider || null, model: provider ? model || null : null, tools, collections }
    try {
      if (persona) await api.changePersona(persona.id, body)
      else await api.createPersona(body)
      onDone(); onClose()
    } catch (e) { setError(text(e)) }
  }
  return (
    <Modal title={persona ? `Edit ${persona.name}` : 'New persona'} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name.trim() || !prompt.trim() || (!!provider && !model)} onClick={save}>Save</button></>}>
      {error && <div className="ask-warning" role="alert">{error}</div>}
      <label className="field"><span>Name</span><input value={name} maxLength={60} onChange={(e) => setName(e.target.value)} /></label>
      <label className="field"><span>What it is for (shown in the picker)</span><input value={description} maxLength={300} onChange={(e) => setDescription(e.target.value)} /></label>
      <label className="field"><span>System prompt: who the assistant is. The chat's mode and the tool rules are added after it.</span>
        <textarea rows={10} value={prompt} maxLength={20000} onChange={(e) => setPrompt(e.target.value)} /></label>
      <label className="field"><span>Mode a new chat starts in</span>
        <select value={mode} onChange={(e) => setMode(e.target.value as Mode | '')}>
          <option value="">Keep the chat's mode</option><option value="plan">plan</option><option value="pentesting">pentesting</option><option value="coding">coding</option>
        </select></label>
      <div className="row">
        <label className="field grow"><span>Preferred provider</span>
          <select value={provider} onChange={(e) => { setProvider(e.target.value); setModel(providers.find((p) => p.name === e.target.value)?.models[0]?.id ?? '') }}>
            <option value="">Keep the chat's model</option>
            {providers.filter((p) => p.configured || p.name === provider).map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          </select></label>
        <label className="field grow"><span>Model</span>
          <select value={model} disabled={!provider} onChange={(e) => setModel(e.target.value)}>
            {model && !models.some((m) => m.id === model) && <option value={model}>{model}</option>}
            {models.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
          </select></label>
      </div>
      <div className="field"><span>Tools it may be offered ({tools.length ? `${tools.length} chosen` : 'all'}). This only narrows the list; permissions still decide every call.</span>
        <div className="persona-tools">{allTools.map((t) => (
          <label key={t} className="check"><input type="checkbox" checked={tools.includes(t)} onChange={() => setTools(flip(tools, t))} /><span className="mono">{t}</span></label>
        ))}</div>
        {tools.length > 0 && <button className="btn small" onClick={() => setTools([])}>Offer all tools</button>}
      </div>
      {allCollections.length > 0 && (
        <div className="field"><span>Knowledge collections attached to its new chats</span>
          <div className="persona-tools">{allCollections.map((c) => (
            <label key={c.id} className="check"><input type="checkbox" checked={collections.includes(c.id)} onChange={() => setCollections(flip(collections, c.id))} /><span>{c.name}</span></label>
          ))}</div></div>
      )}
    </Modal>
  )
}

export function PersonaManager({ onClose }: { onClose: () => void }) {
  const personas = useStore((s) => s.personas)
  const refresh = useStore((s) => s.refreshPersonas)
  const setError = useStore((s) => s.setError)
  const [editing, setEditing] = useState<Persona | 'new' | null>(null)
  useEffect(() => { void refresh() }, [refresh])
  const remove = async (p: Persona) => {
    if (!(await confirmBox(`Delete the persona "${p.name}"? Chats that use it keep working without a persona.`))) return
    try { await api.deletePersona(p.id); await refresh() } catch (e) { setError(text(e)) }
  }
  return (
    <Modal title="Personas" onClose={onClose} footer={<>
      <button className="btn" onClick={() => setEditing('new')}>New persona</button><button className="btn primary" onClick={onClose}>Done</button></>}>
      <div className="hint">A persona is a prompt profile: who the assistant is for a kind of work, with an optional starting mode, model, tool list, and knowledge collections. Pick one in the chat header.</div>
      {personas.map((p) => (
        <div key={p.id} className="mcp-server">
          <div className="row"><b>{p.name}</b>{p.default_mode && <span className="badge">{p.default_mode}</span>}
            {p.model && <span className="muted mono">{p.provider}/{p.model}</span>}
            {p.tools.length > 0 && <span className="muted">{p.tools.length} tools</span>}
            <span className="grow" />
            <button className="btn small" onClick={() => setEditing(p)}>Edit</button>
            <button className="btn small" onClick={() => void remove(p)}>Delete</button></div>
          <div className="hint">{p.description}</div>
        </div>
      ))}
      {personas.length === 0 && <div className="hint">No personas. Create one to give chats a standing role.</div>}
      {editing && <Editor persona={editing === 'new' ? null : editing} onDone={() => void refresh()} onClose={() => setEditing(null)} />}
    </Modal>
  )
}
