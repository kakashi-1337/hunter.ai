import { useEffect, useState } from 'react'
import * as api from '../api'
import { confirmBox } from '../dialog'
import { useStore } from '../store'
import type { Mode, Skill, SkillCandidate, SkillInspection } from '../types'
import { Modal } from './Modal'

const text = (e: unknown) => (e instanceof Error ? e.message : String(e))
const NAME = /^[a-z][a-z0-9-]{0,63}$/

function Editor({ skill, onDone, onClose }: { skill: Skill | null; onDone: () => void; onClose: () => void }) {
  const [name, setName] = useState(skill?.name ?? '')
  const [description, setDescription] = useState(skill?.description ?? '')
  const [body, setBody] = useState(skill?.body ?? '')
  const [mode, setMode] = useState<Mode | ''>(skill?.mode ?? '')
  const [error, setError] = useState<string | null>(null)
  const save = async () => {
    const fields = { name: name.trim(), description, body, mode: mode || null }
    try {
      if (skill) await api.changeSkill(skill.id, fields)
      else await api.writeSkill(fields)
      onDone(); onClose()
    } catch (e) { setError(text(e)) }
  }
  return (
    <Modal wide title={skill ? `Edit /${skill.name}` : 'Write a skill'} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!NAME.test(name.trim()) || !body.trim()} onClick={() => void save()}>Save</button></>}>
      {error && <div className="ask-warning" role="alert">{error}</div>}
      <label className="field"><span>Name: also its slash command. Lower-case letters, digits and hyphens.</span>
        <input className="mono" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} placeholder="recon-notes" /></label>
      <label className="field"><span>When to use it. The model reads this line to decide whether the skill fits a task.</span>
        <input value={description} maxLength={1024} onChange={(e) => setDescription(e.target.value)} placeholder="Use when …" /></label>
      <label className="field"><span>Instructions. The model follows them as your own words.</span>
        <textarea rows={16} value={body} maxLength={60000} onChange={(e) => setBody(e.target.value)} /></label>
      <label className="field"><span>Mode its slash command switches the chat to first</span>
        <select value={mode} onChange={(e) => setMode(e.target.value as Mode | '')}>
          <option value="">Keep the chat's mode</option><option value="plan">plan (nothing can be changed until /go)</option><option value="pentesting">pentesting</option><option value="coding">coding</option>
        </select></label>
    </Modal>
  )
}

/** One skill of a source that was looked at: what it is, what a check of its text found, and the text itself. */
function Candidate({ c, picked, name, onPick, onName }: { c: SkillCandidate; picked: boolean; name: string; onPick: (on: boolean) => void; onName: (v: string) => void }) {
  const [open, setOpen] = useState(false)
  const nameOk = NAME.test(name.trim())
  return (
    <div className="mcp-server">
      <div className="row">
        <label className="check"><input type="checkbox" checked={picked} disabled={!!c.problem} aria-label={`Add ${c.name || 'this skill'}`} onChange={(e) => onPick(e.target.checked)} /><span>/</span></label>
        <input className="mono" value={name} maxLength={64} aria-label="Name of the skill" onChange={(e) => onName(e.target.value)} placeholder="give it a name" style={{ width: 220 }} />
        {c.mode && <span className="badge">{c.mode}</span>}
        {c.files.length > 0 && <span className="muted">{c.files.length} file{c.files.length === 1 ? '' : 's'}</span>}
        <span className="grow" />
        <button className="btn small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? 'Hide the text' : 'Read the text'}</button>
      </div>
      <div className="hint">{c.description || 'No description: the model will not know when to load it by itself.'}</div>
      {c.problem && <div className="ask-warning">{c.problem}</div>}
      {!c.problem && picked && !nameOk && <div className="ask-warning">A name is lower-case letters, digits and hyphens, starting with a letter.</div>}
      {c.taken && name.trim() === c.name && <div className="ask-warning">A skill named {c.name} is already installed. Give this one another name to add it.</div>}
      {c.findings.map((f, i) => (
        <div key={i} className="ask-warning"><span className={`badge ${f.severity === 'high' ? 'bad' : 'warn'}`}>{f.severity}</span> {f.title} (in the {f.where}): <span className="mono">{f.excerpt}</span></div>
      ))}
      {c.left_out.length > 0 && <div className="hint">Not brought along (a link, or over the size limits): <span className="mono">{c.left_out.slice(0, 8).join(', ')}{c.left_out.length > 8 ? ', …' : ''}</span></div>}
      {open && (<>
        <pre className="skill-text">{c.body}</pre>
        {c.files.length > 0 && <div className="hint">Other files the model may read: <span className="mono">{c.files.join(', ')}</span></div>}
      </>)}
    </div>
  )
}

function Adder({ onAdded }: { onAdded: () => void }) {
  const [source, setSource] = useState('')
  const [busy, setBusy] = useState(false)
  const [seen, setSeen] = useState<SkillInspection | null>(null)
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [names, setNames] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const look = async () => {
    setBusy(true); setError(null); setSeen(null)
    try {
      const found = await api.inspectSkills(source)
      setSeen(found)
      setNames(Object.fromEntries(found.skills.map((s) => [s.key, s.name])))
      // One skill is what was asked for; of several, the owner chooses.
      setPicked(Object.fromEntries(found.skills.map((s) => [s.key, found.skills.length === 1 && !s.problem && !s.taken && s.findings.length === 0])))
    } catch (e) { setError(text(e)) } finally { setBusy(false) }
  }
  const chosen = (seen?.skills ?? []).filter((s) => picked[s.key] && !s.problem)
  const ready = chosen.length > 0 && chosen.every((s) => NAME.test((names[s.key] ?? '').trim()) && !(s.taken && (names[s.key] ?? '').trim() === s.name))
  const add = async () => {
    if (!seen) return
    setBusy(true); setError(null)
    try {
      await api.addSkills(seen.id, chosen.map((s) => ({ key: s.key, name: (names[s.key] ?? '').trim() })))
      setSeen(null); setSource('')
      onAdded()
    } catch (e) { setError(text(e)) } finally { setBusy(false) }
  }
  return (
    <div className="field">
      <span>Add a skill</span>
      <textarea rows={2} value={source} aria-label="Where the skill is" onChange={(e) => setSource(e.target.value)}
        placeholder="https://github.com/owner/repo, or the address of one skill's folder in it · /path/to/folder · or paste the text of a SKILL.md" />
      <div className="row">
        <button className="btn" disabled={busy || !source.trim()} onClick={() => void look()}>{busy && !seen ? 'Looking…' : 'Look at it'}</button>
        <span className="hint grow">{seen ? `${seen.skills.length} skill${seen.skills.length === 1 ? '' : 's'} in ${seen.label}. Nothing is added yet.` : 'It is brought here and shown in full first. Nothing in it is run.'}</span>
      </div>
      {error && <div className="ask-warning" role="alert">{error}</div>}
      {seen?.skills.map((c) => (
        <Candidate key={c.key} c={c} picked={!!picked[c.key]} name={names[c.key] ?? ''} onPick={(on) => setPicked({ ...picked, [c.key]: on })} onName={(v) => setNames({ ...names, [c.key]: v })} />
      ))}
      {seen && <div className="row"><span className="hint grow">The model follows a skill as your own instructions. Read what you add.</span>
        <button className="btn primary" disabled={busy || !ready} onClick={() => void add()}>Add {chosen.length || ''} selected</button></div>}
    </div>
  )
}

export function SkillsManager({ onClose }: { onClose: () => void }) {
  const skills = useStore((s) => s.skills)
  const refresh = useStore((s) => s.refreshSkills)
  const setError = useStore((s) => s.setError)
  const [editing, setEditing] = useState<Skill | 'new' | null>(null)
  useEffect(() => { void refresh() }, [refresh])
  const change = async (s: Skill, fields: api.SkillBody) => {
    // The box answers the click at once; the list is read again when the server has it, or when it refused.
    useStore.setState({ skills: skills.map((x) => (x.id === s.id ? { ...x, ...fields } : x)) })
    try { await api.changeSkill(s.id, fields) } catch (e) { setError(text(e)) }
    await refresh()
  }
  const remove = async (s: Skill) => {
    if (!(await confirmBox(`Delete the skill /${s.name}${s.files.length ? ` and its ${s.files.length} file${s.files.length === 1 ? '' : 's'}` : ''}?`))) return
    try { await api.deleteSkill(s.id); await refresh() } catch (e) { setError(text(e)) }
  }
  return (
    <Modal wide title="Skills" onClose={onClose} footer={<>
      <button className="btn" onClick={() => setEditing('new')}>Write a skill</button><button className="btn primary" onClick={onClose}>Done</button></>}>
      <div className="hint">A skill is a set of instructions for a kind of work. Start one by typing its slash command in the message box (<span className="mono">/plan map the login flow</span>), or let the model load it when a task fits its description. Permissions still decide every tool call.</div>
      {skills.map((s) => (
        <div key={s.id} className="mcp-server">
          <div className="row"><b className="mono">/{s.name}</b>{s.mode && <span className="badge" title="Its slash command switches the chat to this mode first">{s.mode}</span>}
            {s.builtin && <span className="muted">built in</span>}
            {s.files.length > 0 && <span className="muted" title={s.files.join('\n')}>{s.files.length} file{s.files.length === 1 ? '' : 's'}</span>}
            <span className="grow" />
            <label className="check" title="Off: no slash command, and the model cannot load it"><input type="checkbox" checked={s.enabled} onChange={(e) => void change(s, { enabled: e.target.checked })} /><span>On</span></label>
            <label className="check" title="Off: it starts only when you type its slash command"><input type="checkbox" checked={s.auto} disabled={!s.enabled} onChange={(e) => void change(s, { auto: e.target.checked })} /><span>The model may load it</span></label>
            <button className="btn small" onClick={() => setEditing(s)}>Edit</button>
            <button className="btn small" onClick={() => void remove(s)}>Delete</button></div>
          <div className="hint">{s.description || 'No description.'}</div>
          {!s.builtin && s.source && <div className="hint">From {s.source}</div>}
        </div>
      ))}
      {skills.length === 0 && <div className="hint">No skills. Add one below, or write your own.</div>}
      <Adder onAdded={() => void refresh()} />
      {editing && <Editor skill={editing === 'new' ? null : editing} onDone={() => void refresh()} onClose={() => setEditing(null)} />}
    </Modal>
  )
}
