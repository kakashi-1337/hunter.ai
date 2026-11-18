import { useEffect, useState } from 'react'
import * as api from '../api'
import { proxyTestText } from '../proxy'
import { useStore } from '../store'
import { localWindowHint } from '../tree'
import type { LocalModel, McpStatus, Settings, ToolInfo } from '../types'
import { McpManager } from './McpManager'
import { MemoryManager } from './MemoryManager'
import { PersonaManager } from './PersonaManager'
import { Modal } from './Modal'
import { PolicyEditor } from './PermissionsPanel'

const KEY_LABELS: Record<string, string> = {
  ABLIT_API_KEY: 'abliteration.ai', ANTHROPIC_API_KEY: 'Anthropic', OPENAI_API_KEY: 'OpenAI',
  XAI_API_KEY: 'xAI', GEMINI_API_KEY: 'Google Gemini',
}

/** One proxy address with its Save and Test buttons. The field holds the address as the server shows it: with the
 * user name, never with the password. Sent back like that, the stored password is kept. */
function ProxyAddress({ label, name, shown, kept, save, providers }: {
  label: string
  name: string                              // what a screen reader calls the field
  shown: string
  kept: boolean                             // a password is stored for it
  save(value: string): Promise<string>      // the address as the server shows it now; rejects with the server's reason
  providers?: boolean                       // it is the proxy for the model providers
}) {
  const [value, setValue] = useState(shown)
  const [error, setError] = useState('')
  const [found, setFound] = useState('')
  const [busy, setBusy] = useState<'' | 'save' | 'test'>('')
  const changed = value.trim() !== shown
  const run = async (what: 'save' | 'test') => {
    setBusy(what)
    setError('')
    setFound('')
    try {
      if (what === 'save') setValue(await save(value))      // a password that was typed leaves the page here
      else setFound(proxyTestText(await api.testProxy(value, undefined, providers)))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }
  return (
    <>
      <div className="keyrow">
        <label className="field">
          <span>{label} · <b className={shown ? 'state-on' : 'state-off'}>{shown ? 'in use' : 'none'}</b>{kept && !changed ? ' · a password is stored and not shown' : ''}</span>
          <input className="mono" spellCheck={false} autoComplete="off" value={value} aria-label={name}
            placeholder="http://127.0.0.1:8080 or socks5://user:password@host:port"
            onChange={(e) => { setFound(''); setValue(e.target.value) }} onKeyDown={(e) => e.key === 'Enter' && changed && void run('save')} />
        </label>
        <div className="row">
          <button className="btn" disabled={!changed || busy !== ''} onClick={() => void run('save')}>{busy === 'save' ? 'Saving…' : value.trim() ? 'Save' : 'Clear'}</button>
          <button className="btn" disabled={!value.trim() || busy !== ''} title="Send one request through it and see what sites see" onClick={() => void run('test')}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
        </div>
      </div>
      {error && <div className="ask-warning" role="alert">{error}</div>}
      {found && <div className="hint" role="status">{found}</div>}
    </>
  )
}

/** The owner's proxies: the one for the agent's traffic to targets, with its switches, and the one for the model
 * providers. The server checks each address and says why it refuses one. */
function ProxyRow({ settings, onSaved }: { settings: Settings; onSaved: (s: Settings) => void }) {
  const [reads, setReads] = useState(settings.upstream_proxy_intercepts)      // the box answers the click at once
  const [crawls, setCrawls] = useState(settings.upstream_proxy_kb)
  const [error, setError] = useState('')
  const put = async (patch: Parameters<typeof api.putProxy>[0]) => {
    const s = await api.putProxy(patch)
    setReads(s.upstream_proxy_intercepts)
    setCrawls(s.upstream_proxy_kb)
    onSaved(s)
    return s
  }
  const flip = async (patch: Parameters<typeof api.putProxy>[0]) => {
    setError('')
    try {
      await put(patch)
    } catch (e) {
      setReads(settings.upstream_proxy_intercepts)       // refused: back to what is saved
      setCrawls(settings.upstream_proxy_kb)
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <>
      <ProxyAddress label="Proxy for the agent's traffic: web_fetch, web_download, the browser and commands" name="Proxy address"
        shown={settings.upstream_proxy} kept={settings.upstream_proxy_password} save={async (value) => (await put({ upstream_proxy: value })).upstream_proxy} />
      {error && <div className="ask-warning" role="alert">{error}</div>}
      <label className="check"><input type="checkbox" checked={reads} disabled={!settings.upstream_proxy}
        onChange={(e) => { setReads(e.target.checked); void flip({ upstream_proxy_intercepts: e.target.checked }) }} />
        <span>It reads https (Burp, mitmproxy, ZAP): accept the certificates it presents. Only for a proxy on this machine. Leave it off for a proxy that only passes traffic on, so a site's certificate is still checked.</span>
      </label>
      <label className="check"><input type="checkbox" checked={crawls} disabled={!settings.upstream_proxy}
        onChange={(e) => { setCrawls(e.target.checked); void flip({ upstream_proxy_kb: e.target.checked }) }} />
        <span>Send knowledge crawls through it too. Off: a web source of the knowledge base is fetched from this machine's own address. A site you mark as local is always fetched directly.</span>
      </label>
      <div className="hint">Your own proxy: Burp, mitmproxy, ZAP, a tunnel, or a proxy service (an http:// or socks5:// address, with a user name and password when it asks for them). It is the default; a target can have a proxy of its own, set in the target. Every host is still resolved and checked first (scope, local addresses); the proxy is then asked for the host by name. When the proxy does not carry a connection, nothing is sent directly. Commands get proxy variables that lead to it, through a local address that holds the login: a program that ignores them, or cannot speak through an HTTP proxy (nmap), connects from this machine itself. Not combined with the Caido switch above.</div>
      <ProxyAddress label="Proxy for the model providers" name="Proxy for the model providers" providers
        shown={settings.provider_proxy} kept={settings.provider_proxy_password} save={async (value) => (await put({ provider_proxy: value })).provider_proxy} />
      <div className="hint">Separate on purpose: what you send to a model does not go through a proxy that a program asked for. Used for Anthropic, Gemini, the OpenAI-compatible providers, and the Claude and Codex programs; written to .env as HUNTERAI_PROVIDER_PROXY. Ollama on this machine is never sent through it.</div>
    </>
  )
}

/** The context window of local models. Emptied, it goes back to the size HunterAI uses by itself. `save` answers
 * with the size in use after the attempt, so a size the server refused does not stay in the field. */
function LocalWindow({ value, save }: { value: number; save(size: number | null): Promise<number> }) {
  const [text, setText] = useState(String(value))
  const [local, setLocal] = useState<LocalModel | null>(null)
  useEffect(() => {          // what this machine's fallback model says of itself: asked when the dialog opens
    let gone = false
    api.systemLocal().then((l) => { if (!gone) setLocal(l) }).catch(() => {})
    return () => { gone = true }
  }, [])
  const { lines, warn } = localWindowHint(local, value)
  const leave = async () => {
    const typed = text.trim()
    if (typed !== String(value)) setText(String(await save(typed ? Number(typed) : null)))
  }
  return (
    <label className="field"><span>Context window for local models (tokens)</span>
      <input type="number" min={2048} step={1024} value={text} onChange={(e) => setText(e.target.value)} onBlur={() => void leave()}
        onKeyDown={(e) => e.key === 'Enter' && void leave()} />
      <span className="hint">What every call to an Ollama model gets, the fallback included. A chat can set its own, and a model never gets more than it takes. A chat that does not fit is shortened for the call to a summary of its early part and its latest messages, and a request that still does not fit is refused with the reason. A larger window keeps more of the chat and needs more memory. Empty: 16,384.</span>
      {lines.map((line) => <span key={line} className="hint">{line}</span>)}
      {warn && <span className="ask-warning" role="alert">{warn}</span>}
    </label>
  )
}

function KeyRow({ name, isSet, onSaved }: { name: string; isSet: boolean; onSaved: () => Promise<void> }) {
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const setError = useStore((s) => s.setError)
  const save = async () => {
    setSaving(true)
    try {
      await api.putKey(name, value)
      setValue('') // the key is never read back; clear it from the page straight away
      await onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="keyrow">
      <label className="field">
        <span>{KEY_LABELS[name] ?? name} <span className="mono">{name}</span> · <b className={isSet ? 'state-on' : 'state-off'}>{isSet ? 'set' : 'not set'}</b></span>
        <input type="password" autoComplete="off" spellCheck={false} value={value} placeholder={isSet ? 'Enter a new key to replace it' : 'Paste key'}
          onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && value.trim() && void save()} />
      </label>
      <button className="btn" disabled={!value.trim() || saving} onClick={save}>{saving ? 'Saving…' : 'Save'}</button>
    </div>
  )
}

export function SettingsDialog({ onClose, onSetup }: { onClose: () => void; onSetup?: () => void }) {
  const providers = useStore((s) => s.providers)
  const refreshProviders = useStore((s) => s.refreshProviders)
  const setError = useStore((s) => s.setError)
  const [settings, setSettings] = useState<Settings | null>(useStore.getState().settings)
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [mcp, setMcp] = useState<McpStatus | null>(null)
  const [managing, setManaging] = useState(false)
  const [personasOpen, setPersonasOpen] = useState(false)
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  useEffect(() => {
    api.listTools().then(setTools).catch(() => {})
    api.mcpStatus().then(setMcp).catch(() => {})
  }, [settings?.permissions])

  const reload = async () => {
    const s = await api.getSettings()
    setSettings(s)
    useStore.setState({ settings: s })
  }
  useEffect(() => { void reload().catch((e) => setError(String(e))) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const update = async (patch: Parameters<typeof api.putSettings>[0]) => {
    try {
      const s = await api.putSettings(patch)
      setSettings(s)
      useStore.setState({ settings: s })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const usable = providers.filter((p) => p.configured)
  const defaultModels = providers.find((p) => p.name === settings?.default_provider)?.models ?? []
  const decisionModels = providers.find((p) => p.name === (settings?.decision_provider ?? settings?.default_provider))?.models ?? []
  const ollamaModels = providers.find((p) => p.name === 'ollama')?.models ?? []

  return (
    <Modal title="Settings" onClose={onClose} footer={<>
      {onSetup && <button className="btn" title="What this install has and what is missing" onClick={onSetup}>Setup check</button>}
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      {!settings ? <div className="muted">Loading…</div> : (
        <>
          <label className="field"><span>Default provider for new chats</span>
            <select value={settings.default_provider}
              onChange={(e) => { const p = providers.find((x) => x.name === e.target.value); void update({ default_provider: e.target.value, default_model: p?.models[0]?.id ?? settings.default_model }) }}>
              {!usable.some((p) => p.name === settings.default_provider) && <option value={settings.default_provider}>{settings.default_provider} (no key set)</option>}
              {usable.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
            </select>
          </label>
          <label className="field"><span>Default model</span>
            <select value={settings.default_model} onChange={(e) => update({ default_model: e.target.value })}>
              {!defaultModels.some((m) => m.id === settings.default_model) && <option value={settings.default_model}>{settings.default_model}</option>}
              {defaultModels.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
            </select>
          </label>
          <label className="field"><span>Fallback model (Ollama) when abliteration.ai is unreachable, times out, or rate-limits</span>
            <select value={settings.fallback_model} onChange={(e) => update({ fallback_model: e.target.value })}>
              {!ollamaModels.some((m) => m.id === settings.fallback_model) && <option value={settings.fallback_model}>{settings.fallback_model}{ollamaModels.length ? ' (not installed)' : ''}</option>}
              {ollamaModels.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
            </select>
            {ollamaModels.length === 0 && <span className="hint">Ollama is not reachable, so the fallback cannot answer right now.</span>}
          </label>
          <LocalWindow key={settings.local_window} value={settings.local_window}
            save={async (size) => { await update({ local_window: size }); return useStore.getState().settings?.local_window ?? settings.local_window }} />
          <div className="row"><span className="hint grow">Personas: prompt profiles for kinds of work (web, mobile, reverse engineering, internal, coding, personal).</span>
            <button className="btn" onClick={() => setPersonasOpen(true)}>Manage personas</button></div>
          {personasOpen && <PersonaManager onClose={() => setPersonasOpen(false)} />}
          <div className="row"><span className="hint grow">Skills: instructions for a kind of work, started with a slash command (/plan, /audit, /debug, /fix, and the ones you add).</span>
            <button className="btn" onClick={() => useStore.getState().setSkillsOpen(true)}>Manage skills</button></div>
          <div className="row"><span className="hint grow">Memory: notes kept across chats, with their source. You can read, correct, and delete every one.</span>
            <button className="btn" onClick={() => setMemoryOpen(true)}>Manage memory</button></div>
          {memoryOpen && <MemoryManager onClose={() => setMemoryOpen(false)} onLeave={onClose} />}
          <h3 className="section">Decisions</h3>
          <label className="check"><input type="checkbox" checked={settings.decision_engine} onChange={(e) => update({ decision_engine: e.target.checked })} />
            <span>Use the built-in decision engine: the decide_* tools and the risk gate. Switch it off if you have a JEV subscription and use the JEV MCP server instead (add it under "Manage MCP servers" with your own key); its tools then take the place of the built-in ones.</span>
          </label>
          <div className="hint">The decision model answers the agent's yes/no, choice, and score questions, checks what a subagent claims, and screens risky calls. A small fast model is enough.</div>
          <label className="field"><span>Decision provider</span>
            <select value={settings.decision_provider ?? ''}
              onChange={(e) => { const p = providers.find((x) => x.name === e.target.value); void update({ decision_provider: e.target.value, decision_model: e.target.value ? p?.models[0]?.id ?? '' : '' }) }}>
              <option value="">Same as new chats ({settings.default_provider})</option>
              {usable.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
            </select>
          </label>
          <label className="field"><span>Decision model</span>
            <select value={settings.decision_model ?? ''} disabled={!settings.decision_provider} onChange={(e) => update({ decision_model: e.target.value })}>
              {!settings.decision_provider && <option value="">Same as new chats ({settings.default_model})</option>}
              {settings.decision_model && !decisionModels.some((m) => m.id === settings.decision_model) && <option value={settings.decision_model}>{settings.decision_model}</option>}
              {decisionModels.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
            </select>
          </label>
          <label className="check"><input type="checkbox" checked={settings.answer_check} onChange={(e) => update({ answer_check: e.target.checked })} />
            <span>Check each answer against the tool results of its run: after a run in a chat with tools, the decision model lists the statements of the answer that no tool result shows, or that one contradicts, and they are marked under the answer. One more call to the decision model per run.</span></label>
          <label className="check"><input type="checkbox" checked={settings.decision_gate && settings.decision_engine} disabled={!settings.decision_engine} onChange={(e) => update({ decision_gate: e.target.checked })} />
            <span>Risk gate: screen every write, edit, and execute call first. It is advice: it can make an allowed call ask, and it never approves one. Costs one decision-model call per screened call.</span>
          </label>
          <h3 className="section">Images</h3>
          <label className="field"><span>Local model that reads images for the look_at_image tool (an Ollama model name; empty: the first installed one that can)</span>
            <input className="mono" defaultValue={settings.vision_model ?? ''} placeholder="automatic" onBlur={(e) => { if (e.target.value.trim() !== (settings.vision_model ?? '')) update({ vision_model: e.target.value.trim() }) }} /></label>
          <div className="hint">A chat model without image input can ask this model to describe a screenshot. It runs on this machine, one local model at a time, and is unloaded after each answer.</div>
          <h3 className="section">Caido</h3>
          <label className="check"><input type="checkbox" checked={settings.browser_via_caido} disabled={!settings.caido_available}
            onChange={(e) => update({ browser_via_caido: e.target.checked })} />
            <span>Send the browser and web_fetch through Caido, so that every request a page makes and every page the agent fetches is recorded there and the caido tools can read and replay it. Caido reads https by answering with its own certificates, so the browser and web_fetch stop checking a site's certificate while this is on. For the browser it applies to sessions opened from now on. Downloads are not recorded. A shell command is recorded when it is sent through Caido: while this is on, every command has Caido's address in the variable CAIDO_PROXY, and the model is told to give it to a scanner's own proxy switch (httpx -http-proxy, nuclei -proxy).</span>
          </label>
          <div className="hint">{settings.caido_available
            ? 'HunterAI runs its own Caido on this machine; open http://127.0.0.1:8791 and continue as a guest to watch the traffic.'
            : 'caido-cli was not found on this machine (PATH, or ~/.hunterai/bin/caido-cli).'}</div>
          <h3 className="section">Proxy</h3>
          <ProxyRow settings={settings} onSaved={(s) => { setSettings(s); useStore.setState({ settings: s }) }} />
          <h3 className="section">Knowledge base</h3>
          <label className="check"><input type="checkbox" checked={settings.kb_redact} onChange={(e) => update({ kb_redact: e.target.checked })} />
            <span>Mask strings shaped like keys, tokens, and passwords before a document is indexed. Retrieved passages are sent to the model provider; turn this off only for material where the secrets are what you are looking for. It applies to documents ingested from now on.</span>
          </label>
          <h3 className="section">Tools and permissions</h3>
          <div className="hint">Defaults for every chat. A chat's own settings and its mode come first; plan mode never writes or executes.</div>
          <PolicyEditor policy={settings.permissions ?? {}} tools={tools}
            inherit={(risk) => `Mode default (${settings.presets?.pentesting?.[risk] ?? 'ask'} in pentesting)`}
            onChange={(permissions) => update({ permissions })} />
          <label className="field"><span>Max model turns per run (1 to 200)</span>
            <input type="number" min={1} max={200} defaultValue={settings.max_turns}
              onBlur={(e) => { const n = Number(e.target.value); if (n !== settings.max_turns) void update({ max_turns: n }) }} />
          </label>
          <label className="field"><span>Subagents of a swarm working at the same time (1 to {settings.swarm_width_max})</span>
            <input type="number" min={1} max={settings.swarm_width_max} defaultValue={settings.swarm_width}
              onBlur={(e) => { const n = Number(e.target.value); if (n !== settings.swarm_width) void update({ swarm_width: n }) }} />
            <span className="hint">Each one runs its own tools and model calls, so more of them load this machine and your model provider's rate limit. Raise it on a machine with cores and memory to spare; lower it when a swarm makes the machine slow.
              {settings.machine.cores ? ` This machine has ${settings.machine.cores} processor cores${settings.machine.memory_gb ? ` and ${settings.machine.memory_gb} GB of memory` : ''}.` : ''}</span>
          </label>
          <div className="field"><span>MCP servers</span>
            {mcp?.error && <span className="hint">{mcp.error}</span>}
            <div className="row">
              <span className="hint grow">{mcp ? `${mcp.configured.filter((c) => c.enabled && c.expose).length} on, ${mcp.configured.filter((c) => !c.enabled).length} off` : ''}</span>
              <button className="btn" onClick={() => setManaging(true)}>Manage MCP servers</button>
            </div>
            <label className="check"><input type="checkbox" checked={settings.mcp_on_demand} onChange={(e) => update({ mcp_on_demand: e.target.checked })} />
              <span>Load the tools of large MCP servers on demand: a server with more than 15 tools is not put in the tool list of every request. The model is told the names, looks one up with mcp_find, and runs it with mcp_call, under the same checks and questions as a direct call. Saves tens of thousands of tokens per request with a server like rea.</span></label>
          </div>
          {managing && <McpManager onClose={() => { setManaging(false); api.mcpStatus().then(setMcp).catch(() => {}); api.listTools().then(setTools).catch(() => {}) }} />}
          <h3 className="section">Notifications</h3>
          <label className="check"><input type="checkbox" checked={settings.notify_findings} onChange={(e) => update({ notify_findings: e.target.checked })} />
            <span>Announce positive findings in a Discord channel: a finding the triage does not doubt, or one set to "confirmed". Sent once per finding: its title, severity, status, the target's name, and the triage figures. The evidence is never sent.</span></label>
          <label className="field"><span>Least severity to announce</span>
            <select value={settings.notify_min_severity} onChange={(e) => update({ notify_min_severity: e.target.value as Settings['notify_min_severity'] })}>
              {['info', 'low', 'medium', 'high', 'critical'].map((s) => <option key={s} value={s}>{s}</option>)}</select></label>
          <KeyRow name="DISCORD_WEBHOOK_URL" isSet={settings.notify_webhook_set} onSaved={reload} />
          <div className="row">
            <span className="hint grow">{notice ?? (settings.notify_webhook_set ? 'A Discord webhook address is set in .env.' : 'Paste a Discord webhook address (channel settings → Integrations → Webhooks). It is a secret: whoever has it can post to that channel.')}</span>
            <button className="btn small" disabled={!settings.notify_webhook_set} onClick={() => { setNotice('Sending…'); api.notifyTest().then((r) => setNotice(r.ok ? 'Test message sent: look at the channel.' : `Not sent: ${r.detail}`)).catch((e) => setNotice(String(e))) }}>Send a test message</button>
          </div>
          <h3 className="section">API keys</h3>
          <div className="hint">API keys are written to the project's .env file and are never shown again. Other lines in that file are left untouched.</div>
          {Object.entries(settings.keys).map(([name, isSet]) => (
            <KeyRow key={name} name={name} isSet={isSet} onSaved={async () => { await reload(); await refreshProviders() }} />
          ))}
        </>
      )}
    </Modal>
  )
}
