import { useEffect, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import { parseLines } from '../tree'
import type { Decision, Grant, Policy, Risk, ToolInfo } from '../types'
import { Modal } from './Modal'

const RISKS: Risk[] = ['read', 'write', 'execute', 'network']
const LISTS: [keyof Policy & ('read_paths' | 'write_paths' | 'domains'), string, string][] = [
  ['read_paths', 'Folders that may be read without asking', '/absolute/path, one per line'],
  ['write_paths', 'Folders that may be written without asking', '/absolute/path, one per line'],
  ['domains', 'Hosts that may be contacted without asking', 'example.com, *.example.com, 10.0.0.0/24, host:port'],
]

function Lines({ label, hint, value, onSave }: { label: string; hint: string; value: string[]; onSave: (v: string[]) => void }) {
  const [text, setText] = useState(value.join('\n'))
  useEffect(() => setText(value.join('\n')), [value.join('\n')]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <label className="field"><span>{label}</span>
      <textarea rows={2} value={text} placeholder={hint} onChange={(e) => setText(e.target.value)}
        onBlur={() => { const next = parseLines(text); if (next.join('\n') !== value.join('\n')) onSave(next) }} />
    </label>
  )
}

/**
 * The editable part of a policy: one decision per risk class, per-tool overrides, and the three
 * allowlists. `locked` risk classes are shown as denied and cannot be changed (plan mode).
 */
export function PolicyEditor({ policy, tools, inherit, locked, onChange }: {
  policy: Policy
  tools: ToolInfo[]
  inherit: (risk: Risk) => string
  locked?: Risk[]
  onChange: (next: Policy) => void
}) {
  const setRisk = (risk: Risk, v: string) => {
    const next = { ...(policy.risk ?? {}) }
    if (v) next[risk] = v as Decision
    else delete next[risk]
    onChange({ ...policy, risk: next })
  }
  const setTool = (name: string, v: string) => {
    const next = { ...(policy.tools ?? {}) }
    if (v) next[name] = v as Decision
    else delete next[name]
    onChange({ ...policy, tools: next })
  }
  return (
    <>
      <div className="perm-grid">
        {RISKS.map((risk) => (
          <label key={risk} className="field"><span>{risk}</span>
            {locked?.includes(risk) ? <select disabled value="deny"><option value="deny">Denied in plan mode</option></select> : (
              <select aria-label={`Decision for ${risk}`} value={policy.risk?.[risk] ?? ''} onChange={(e) => setRisk(risk, e.target.value)}>
                <option value="">{inherit(risk)}</option>
                <option value="allow">Allow</option>
                <option value="ask">Ask</option>
                <option value="deny">Deny</option>
              </select>
            )}
          </label>
        ))}
      </div>
      {tools.length > 0 && (
        <table className="perm-tools">
          <thead><tr><th>Tool</th><th>Risk</th><th>Now</th><th>Override</th></tr></thead>
          <tbody>
            {tools.map((t) => (
              <tr key={t.name} title={t.description}>
                <td className="mono">{t.name}{t.source !== 'native' && <span className="muted"> ({t.source})</span>}</td>
                <td>{t.risk}</td>
                <td><span className={`badge ${t.decision === 'deny' ? 'bad' : t.decision === 'ask' ? 'warn' : ''}`}>{t.decision}</span> <span className="muted">{t.rule}</span></td>
                <td>
                  {t.rule === 'mode' ? <span className="muted">fixed by the mode</span> : (
                    <select aria-label={`Override for ${t.name}`} value={policy.tools?.[t.name] ?? ''} onChange={(e) => setTool(t.name, e.target.value)}>
                      <option value="">—</option><option value="allow">Allow</option><option value="ask">Ask</option><option value="deny">Deny</option>
                    </select>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {LISTS.map(([key, label, hint]) => (
        <Lines key={key} label={label} hint={hint} value={policy[key] ?? []} onSave={(v) => onChange({ ...policy, [key]: v })} />
      ))}
    </>
  )
}

export function PermissionsPanel({ onClose }: { onClose: () => void }) {
  const chat = useStore((s) => s.chat)
  const inUse = useStore((s) => s.workdir)
  const settings = useStore((s) => s.settings)
  const setError = useStore((s) => s.setError)
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [grants, setGrants] = useState<Grant[]>([])
  const [folder, setFolder] = useState(chat?.workdir ?? '')
  const chatId = chat?.id

  const reload = async () => {
    if (!chatId) return
    try {
      const [t, g, c] = await Promise.all([api.listTools(chatId), api.listGrants(chatId), api.getChat(chatId)])
      setTools(t)
      setGrants(g)
      if (useStore.getState().activeChatId === chatId) useStore.setState({ chat: c.chat, workdir: c.workdir ?? null })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  useEffect(() => { void reload() }, [chatId, chat?.mode, chat?.target_id]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!chat) return null

  /** Every change is one update of the chat, then a reload (the folder in use and the grants may have changed). */
  const save = async (patch: Parameters<typeof api.updateChat>[1]) => {
    try {
      await api.updateChat(chat.id, patch)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
    await reload()
  }
  const preset = settings?.presets?.[chat.mode]
  const global = settings?.permissions ?? {}

  return (
    <Modal title="Permissions for this chat" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
      <div className="field"><span>Working folder in use</span><code className="mono">{inUse ?? '…'}</code></div>
      <label className="field"><span>Use another folder (absolute path; empty = the default)</span>
        <div className="row">
          <input className="grow" aria-label="Working folder" value={folder} placeholder="/home/you/project" onChange={(e) => setFolder(e.target.value)} />
          <button className="btn" onClick={() => save({ workdir: folder.trim() || null })}>Save</button>
          <button className="btn" onClick={() => { setFolder(''); void save({ workdir: null }) }}>Reset to default</button>
        </div>
      </label>
      <PolicyEditor policy={chat.permissions ?? {}} tools={tools} locked={chat.mode === 'plan' ? ['write', 'execute'] : []}
        inherit={(risk) => `Mode default (${global.risk?.[risk] ?? preset?.[risk] ?? 'ask'})`}
        onChange={(permissions) => save({ permissions })} />
      <div className="field">
        <span>Allowed for this chat ({grants.length})</span>
        {grants.length === 0 ? <span className="hint">Nothing yet. "Allow for this chat" answers appear here; changing the mode or target clears them.</span> : (
          <>
            <ul className="grants">{grants.map((g) => <li key={`${g.tool}\n${g.target}`}><span className="mono">{g.tool}</span> <code>{g.target}</code></li>)}</ul>
            <div><button className="btn danger" onClick={async () => { await api.clearGrants(chat.id); await reload() }}>Clear all</button></div>
          </>
        )}
      </div>
    </Modal>
  )
}
