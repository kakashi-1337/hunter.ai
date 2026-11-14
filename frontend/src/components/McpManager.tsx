import { useEffect, useState } from 'react'
import * as api from '../api'
import { useStore } from '../store'
import { commandLine, parseArgs, scanSummary, scanToolLine, visible } from '../tree'
import type { McpCheck, McpInspection, McpScan, McpScanTool, McpServer, McpStatus, Risk } from '../types'
import { confirmBox } from '../dialog'
import { Modal } from './Modal'

const RISKS: [Risk, string][] = [['execute', 'execute: every call asks (use this for code you have not read)'], ['network', 'network'], ['write', 'write'], ['read', 'read: calls run without asking']]
const LEVEL: Record<string, string> = { danger: 'bad', caution: 'warn', 'nothing-found': '', unchecked: 'warn' }
const text = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** What the scanner read in the tool definitions a running server sent, and the owner's approval of held ones. */
function ScanPanel({ scan, busy, onApprove }: { scan: McpScan; busy: boolean; onApprove: (tools: Record<string, string>) => void }) {
  const [read, setRead] = useState<Record<string, boolean>>({})
  const r = scan.report
  if (!r) return <div className="mcp-check">Nothing to show: the scanner reads a server's tool definitions when it starts. Switch it on first.</div>
  const order = (t: McpScanTool) => (t.held ? 0 : t.findings.length ? 1 : 2)
  const tools = [...r.tools].sort((a, b) => order(a) - order(b) || a.name.localeCompare(b.name))
  return (
    <div className="mcp-check">
      <div><b>Scan:</b> {scanSummary(r)}</div>
      {r.note && <div className="warn-line">{r.note}</div>}
      <div className="hint">The scanner reads names, descriptions, and argument schemas for known shapes of a poisoned tool. Nothing found is not proof of safety: it cannot see what a tool does when it runs.</div>
      {tools.map((t) => (
        <details key={t.name} className="mcp-finding" open={t.held}>
          <summary><b className="mono">{visible(t.name)}</b> <span className={`badge ${t.held ? 'bad' : t.findings.some((f) => f.severity !== 'info') ? 'warn' : ''}`}>{scanToolLine(t)}</span></summary>
          {t.findings.map((f, i) => (
            <div key={i}><span className={`badge ${f.severity === 'high' ? 'bad' : f.severity === 'medium' ? 'warn' : ''}`}>{f.severity}</span> {f.title} <span className="muted">({f.where})</span>
              <pre>{visible(f.excerpt)}</pre></div>
          ))}
          {t.previous !== undefined && <><div className="muted">What you accepted before:</div><pre>{visible(t.previous) || '(no description)'}</pre>
            {t.previous_schema !== t.schema && <pre>{visible(t.previous_schema ?? '') || '(no schema was kept)'}</pre>}<div className="muted">What it says now:</div></>}
          <pre>{visible(t.description) || '(no description)'}</pre>
          <details open={t.held && t.previous !== undefined && t.previous_schema !== t.schema}><summary>Arguments (the schema the model is given)</summary><pre>{visible(t.schema ?? '')}</pre></details>
          {t.held && (<div className="row">
            <label className="check"><input type="checkbox" aria-label={`I have read ${t.name}`} checked={!!read[t.digest]} onChange={(e) => setRead((cur) => ({ ...cur, [t.digest]: e.target.checked }))} />I have read this definition and its arguments</label>
            <button className="btn small" disabled={busy || !read[t.digest]} onClick={() => onApprove({ [t.name]: t.digest })}>Accept and offer to the model</button>
          </div>)}
        </details>
      ))}
      {r.removed.length > 0 && <div className="muted">Accepted earlier, no longer sent by the server: {r.removed.join(', ')}</div>}
    </div>
  )
}

/** Step by step: where the server comes from, what the check found, the exact commands, approve. */
function AddWizard({ onDone, onClose }: { onDone: () => void; onClose: () => void }) {
  const [source, setSource] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [ins, setIns] = useState<McpInspection | null>(null)
  const [name, setName] = useState('')
  const [risk, setRisk] = useState<Risk>('execute')
  const [args, setArgs] = useState('')
  const [env, setEnv] = useState<Record<string, string>>({})
  const [read, setRead] = useState(false)

  const run = async (label: string, fn: () => Promise<void>) => { setBusy(label); setError(null); try { await fn() } catch (e) { setError(text(e)) } finally { setBusy('') } }
  const inspect = () => run('Fetching and checking… nothing from it is run', async () => {
    const r = await api.mcpInspect(source)
    setIns(r); setName(r.proposal.name); setArgs(commandLine(null, r.proposal.args)); setRead(false); setRisk('execute')
    setEnv(Object.fromEntries(r.proposal.env_needed.map((k) => [k, ''])))
  })
  const review = () => run('Asking the decision model…', async () => { if (ins) setIns({ ...ins, review: await api.mcpReview(ins.id) }) })
  const install = () => run(ins?.proposal.install.length ? 'Installing… this can take a few minutes' : 'Adding…', async () => {
    if (!ins) return
    await api.mcpInstall(ins.id, { name, risk, env, approved_install: ins.proposal.install, args: parseArgs(args) })
    onDone(); onClose()
  })
  const p = ins?.proposal
  const high = ins?.report?.counts.high ?? 0

  return (
    <Modal title="Add an MCP server" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      {ins && <button className="btn primary" disabled={!!busy || !name.trim() || (high > 0 && !read)} onClick={install}>
        {p?.install.length ? 'Approve: run these steps and add' : 'Approve and add'}</button>}</>}>
      <label className="field"><span>Where it comes from</span>
        <textarea rows={ins?.kind === 'config' ? 4 : 2} className="mono" value={source} placeholder={'https://github.com/owner/repo  ·  npm:@scope/package  ·  pypi:package  ·  /path/to/folder  ·  or paste a {"mcpServers": …} configuration'}
          disabled={!!busy} onChange={(e) => { setSource(e.target.value); setIns(null) }} /></label>
      <div className="row"><button className="btn" disabled={!source.trim() || !!busy} onClick={inspect}>Inspect</button>
        <span className="hint grow">{busy || 'The code is downloaded and read. Nothing is installed or started until you approve.'}</span></div>
      {error && <div className="ask-warning" role="alert" style={{ whiteSpace: 'pre-wrap' }}>{error}</div>}
      {ins && p && (<>
        <h3 className="section">Check result{ins.label ? ` for ${ins.label}` : ''}</h3>
        <div className={`mcp-verdict ${LEVEL[ins.verdict.level]}`}>{ins.verdict.text}</div>
        {ins.report && <div className="hint">{ins.report.read} of {ins.report.files} files read · high {ins.report.counts.high} · medium {ins.report.counts.medium} · info {ins.report.counts.info}
          {ins.audit ? (ins.audit.checked ? ` · ${ins.audit.checked} dependency versions checked, ${ins.audit.vulnerable.length} with known vulnerabilities` : ' · dependencies not audited (no pinned versions found)') : ''}</div>}
        {ins.report?.not_unpacked?.length ? <details><summary className="warn-line">{ins.report.not_unpacked.length} entries of the package were not unpacked or read</summary>{ins.report.not_unpacked.map((t) => <div key={t} className="mono mcp-vuln">{visible(t)}</div>)}</details> : null}
        {ins.report?.findings.length ? (
          <details open={high > 0}><summary>Findings ({ins.report.findings.length})</summary>
            {ins.report.findings.map((f, i) => (
              <div key={i} className="mcp-finding"><span className={`badge ${f.severity === 'high' ? 'bad' : f.severity === 'medium' ? 'warn' : ''}`}>{f.severity}</span> {f.title}
                <div className="mono muted">{visible(f.file)}:{f.line}</div><pre>{visible(f.excerpt)}</pre></div>
            ))}
            {Object.entries(ins.report.more).map(([rule, n]) => <div key={rule} className="hint">…and {n} more of “{rule}”.</div>)}
          </details>) : null}
        {ins.audit?.vulnerable.length ? (
          <details><summary>Dependencies with known vulnerabilities ({ins.audit.vulnerable.length})</summary>
            {ins.audit.vulnerable.map((v) => <div key={`${v.name}@${v.version}`} className="mono mcp-vuln">{v.name}@{v.version} <span className="muted">{v.ids.join(', ')}</span></div>)}
          </details>) : null}
        {ins.report && (<div className="row">
          <button className="btn small" disabled={!!busy} onClick={review}>Ask the decision model to read the findings</button>
          {ins.review && <span className={ins.review.advice === 'install' && !ins.review.invalid ? 'hint' : 'warn-line'}>
            Model's advice (not a fact): {ins.review.invalid ? 'no usable answer' : `${ins.review.advice.replace('_', ' ')} · malicious ${ins.review.malicious_probability}`}</span>}
        </div>)}
        <h3 className="section">What will run</h3>
        {p.install.length > 0 && (<><div className="hint">Install steps, in {visible(ins.root ?? '')}, with install scripts off and none of your API keys:</div>
          {p.install.map((s, i) => (<div key={i}><pre className="ask-args">{visible(commandLine(s[0], s.slice(1)))}</pre>
            {p.install_runs_code?.[i] && <div className="ask-warning" role="alert">{visible(p.install_runs_code[i]!)}</div>}</div>))}</>)}
        <div className="hint">Start command (runs when you switch the server on):</div>
        {p.transport === 'http'
          ? <pre className="ask-args">{visible(p.url ?? '')}</pre>
          : <div className="row"><span className="mono">{visible(p.command ?? '')}</span><input className="grow mono" aria-label="Arguments" value={args} onChange={(e) => setArgs(e.target.value)} /></div>}
        {visible(args) !== args && <div className="ask-warning" role="alert">The arguments hold invisible or control characters: {visible(args)}</div>}
        {p.notes.map((n) => <div key={n} className="hint">{visible(n)}</div>)}
        {ins.kind === 'folder' && <div className="hint">This server runs from that folder as it is at the time: whatever can write there changes what runs.</div>}
        <label className="field"><span>Name (its tools appear as name_tool)</span><input value={name} maxLength={24} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} /></label>
        <label className="field"><span>Risk class of its tools</span>
          <select value={risk} onChange={(e) => setRisk(e.target.value as Risk)}>{RISKS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select></label>
        {Object.keys(env).length > 0 && <div className="hint">Settings the code reads from its environment. Fill in the ones it needs; they are stored in mcp.json on this machine and never shown again.</div>}
        {Object.keys(env).map((k) => (
          <label key={k} className="field"><span className="mono">{k}</span>
            <input type="password" autoComplete="off" spellCheck={false} value={env[k]} onChange={(e) => setEnv({ ...env, [k]: e.target.value })} /></label>
        ))}
        {high > 0 && <label className="check"><input type="checkbox" checked={read} onChange={(e) => setRead(e.target.checked)} /><span>I have read the high-risk findings above and still want to add this server.</span></label>}
        <div className="hint">The server is added switched off. Switching it on starts its code on this machine{p.install_runs_code?.some(Boolean) ? '; a step marked above already runs some of it during install' : ''}.</div>
      </>)}
    </Modal>
  )
}

function EditDialog({ server, onDone, onClose }: { server: McpServer | null; onDone: () => void; onClose: () => void }) {
  const [name, setName] = useState(server?.name ?? '')
  const [command, setCommand] = useState(server?.command ?? '')
  const [args, setArgs] = useState(commandLine(null, server?.args ?? []))
  const [url, setUrl] = useState(server?.url ?? '')
  const [risk, setRisk] = useState<Risk>(server?.risk ?? 'execute')
  const [env, setEnv] = useState<[string, string][]>((server?.env_names ?? []).map((k) => [k, '']))
  // An http server with no header yet gets the usual one to fill in: the key goes here, not under Environment.
  const [headers, setHeaders] = useState<[string, string][]>(
    server?.header_names?.length ? server.header_names.map((k) => [k, '']) : [['Authorization', '']])
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const save = async () => {
    const http = !command.trim() && !!url.trim()
    // A value typed next to an empty name would be dropped without a word: say so instead.
    if ((http ? headers : env).some(([k, v]) => !k.trim() && v !== '')) {
      setError(http ? 'A header needs a name next to its value, for example Authorization.' : 'A variable needs a name next to its value.')
      return
    }
    const body = { transport: http ? 'http' as const : 'stdio' as const, command: http ? null : command.trim(), args: parseArgs(args), url: http ? url.trim() : null, risk,
      env: http ? {} : Object.fromEntries(env.filter(([k]) => k.trim()).map(([k, v]) => [k.trim(), v === '' && server?.env_names.includes(k) ? null : v])),
      headers: http ? Object.fromEntries(headers.filter(([k, v]) => k.trim() && (v !== '' || server?.header_names?.includes(k)))
        .map(([k, v]) => [k.trim(), v === '' && server?.header_names?.includes(k) ? null : v])) : {} }
    // A server that is on is restarted with the new settings before the answer comes: that can take a while.
    setSaving(true); setError(null)
    try {
      if (server) await api.mcpChange(server.name, body)
      else await api.mcpAdd({ name, ...body })
      onDone(); onClose()
    } catch (e) { setError(text(e)) } finally { setSaving(false) }
  }
  return (
    <Modal title={server ? `Edit ${server.name}` : 'Add a server by hand'} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={saving || !(server || name.trim()) || !(command.trim() || url.trim())} onClick={save}>{saving ? 'Saving…' : 'Save'}</button></>}>
      {!server && <label className="field"><span>Name</span><input value={name} maxLength={24} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} /></label>}
      <label className="field"><span>Command (leave empty for an http server)</span><input className="mono" value={command} onChange={(e) => setCommand(e.target.value)} /></label>
      <label className="field"><span>Arguments (spaces separate; use double quotes around one with spaces)</span><input className="mono" value={args} onChange={(e) => setArgs(e.target.value)} /></label>
      <label className="field"><span>URL (http servers only)</span><input className="mono" value={url} onChange={(e) => setUrl(e.target.value)} /></label>
      <label className="field"><span>Risk class of its tools</span>
        <select value={risk} onChange={(e) => setRisk(e.target.value as Risk)}>{RISKS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select></label>
      {!!command.trim() && <div className="field"><span>Environment of the command (a stored value is kept when its box is left empty)</span>
        {env.map(([k, v], i) => (
          <div key={i} className="row"><input className="mono" placeholder="NAME" value={k} onChange={(e) => setEnv(env.map((x, j) => (j === i ? [e.target.value, x[1]] : x)))} />
            <input className="grow" type="password" autoComplete="off" placeholder={server?.env_names.includes(k) ? '•••••••• saved: leave empty to keep it, type to replace' : 'value'} value={v}
              onChange={(e) => setEnv(env.map((x, j) => (j === i ? [x[0], e.target.value] : x)))} />
            <button className="icon" aria-label="Remove" onClick={() => setEnv(env.filter((_, j) => j !== i))}>✕</button></div>
        ))}
        <button className="btn small" onClick={() => setEnv([...env, ['', '']])}>+ Variable</button></div>}
      {!command.trim() && <div className="field"><span>Headers sent to the http server: its API key goes here (a stored value is kept when its box is left empty)</span>
        {headers.map(([k, v], i) => (
          <div key={i} className="row"><input className="mono" placeholder="Header-Name" value={k} onChange={(e) => setHeaders(headers.map((x, j) => (j === i ? [e.target.value, x[1]] : x)))} />
            <input className="grow" type="password" autoComplete="off" placeholder={server?.header_names?.includes(k) ? '•••••••• saved: leave empty to keep it, type to replace' : 'Bearer <key>, or Bearer ${NAME} for a key in .env'} value={v}
              onChange={(e) => setHeaders(headers.map((x, j) => (j === i ? [x[0], e.target.value] : x)))} />
            <button className="icon" aria-label="Remove header" onClick={() => setHeaders(headers.filter((_, j) => j !== i))}>✕</button></div>
        ))}
        <button className="btn small" onClick={() => setHeaders([...headers, ['', '']])}>+ Header</button>
        <div className="hint">An http server runs somewhere else: it sees every argument the model sends to its tools. Headers go only over https.</div></div>}
      <div className="hint">Saving restarts the server if it is on. Starting it runs its command on this machine. Saved values are never shown again: the list says which names are set.</div>
      {error && <div className="ask-warning" role="alert">{error}</div>}
    </Modal>
  )
}

/** Every MCP server: state, switch, check, edit, remove; and the way in for new ones. */
export function McpManager({ onClose }: { onClose: () => void }) {
  const setGlobalError = useStore((s) => s.setError)
  const [servers, setServers] = useState<McpServer[]>([])
  const [status, setStatus] = useState<McpStatus | null>(null)
  const [checks, setChecks] = useState<Record<string, McpCheck>>({})
  const [scans, setScans] = useState<Record<string, McpScan>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [dialog, setDialog] = useState<'add' | 'manual' | McpServer | null>(null)
  const reload = async () => {
    try { const [a, b] = await Promise.all([api.mcpServers(), api.mcpStatus()]); setServers(a.servers); setStatus(b) } catch (e) { setGlobalError(text(e)) }
  }
  useEffect(() => { void reload() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (name: string, fn: () => Promise<unknown>) => { setBusy(name); try { await fn() } catch (e) { setGlobalError(text(e)) } finally { setBusy(null); await reload() } }
  const scan = (name: string) => act(name, async () => { const r = await api.mcpScan(name); setScans((cur) => ({ ...cur, [name]: r })) })
  const approve = (name: string, tools: Record<string, string>) => act(name, async () => { const r = await api.mcpApprove(name, tools); setScans((cur) => ({ ...cur, [name]: r })) })
  const check = (name: string) => act(name, async () => { const c = await api.mcpCheck(name); setChecks((cur) => ({ ...cur, [name]: c })) })

  return (
    <Modal title="MCP servers" onClose={onClose} footer={<>
      <button className="btn" onClick={() => setDialog('manual')}>Add by hand</button>
      <button className="btn" onClick={() => setDialog('add')}>Add from a repo, package, or config</button>
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      <div className="hint">A server that is on adds its tools to every request and runs as a program on this machine. Its calls follow the risk class shown.</div>
      {servers.map((s) => {
        const st = status?.servers.find((x) => x.name === s.name)
        const c = checks[s.name]
        return (
          <div key={s.name} className="mcp-server">
            <div className="row">
              <label className="check"><input type="checkbox" aria-label={`Switch ${s.name}`} checked={s.enabled} disabled={!s.expose || busy === s.name}
                onChange={(e) => void act(s.name, () => api.mcpSwitch(s.name, e.target.checked))} /><b className="mono">{s.name}</b></label>
              <span className="badge">{s.risk}</span>
              <span className={`badge ${!s.enabled || st?.state === 'ready' ? '' : 'warn'}`}>{busy === s.name ? 'working…' : s.enabled ? st?.state ?? 'not started' : 'off'}</span>
              {s.enabled && st && <span className="muted">{st.tools.length} tools</span>}
              {s.enabled && (st?.held?.length ?? 0) > 0 && <span className="badge bad">{st!.held!.length} held by the scanner</span>}
              {s.bundled && <span className="muted">bundled</span>}
              <span className="grow" />
              <button className="btn small" disabled={busy === s.name} onClick={() => void check(s.name)}>Check</button>
              <button className="btn small" disabled={busy === s.name} onClick={() => void scan(s.name)}>Scan</button>
              {!s.bundled && <button className="btn small" onClick={() => setDialog(s)}>Edit</button>}
              {!s.bundled && <button className="btn small" onClick={() => void confirmBox(`Remove the server "${s.name}"? Its code on disk is not deleted.`).then((ok) => { if (ok) void act(s.name, () => api.mcpDelete(s.name)) })}>Remove</button>}
            </div>
            <div className="mono muted kb-wrap">{visible(s.transport === 'http' ? s.url ?? '' : commandLine(s.command, s.args))}</div>
            {s.env_names.length > 0 && <div className="hint">Environment set: {s.env_names.join(', ')}</div>}
            {(s.header_names?.length ?? 0) > 0 && <div className="hint">Headers set: {s.header_names!.join(', ')}</div>}
            {s.enabled && st?.error && !c && <div className="warn-line">{visible(st.error)}</div>}
            {c && (<div className="mcp-check">
              <div>{c.state === 'ready' ? `Ready · ${c.tools.length} tools · ping ${c.ping_ms ?? '—'} ms` : `State: ${c.state}${c.error ? ` · ${c.error}` : ''}`}</div>
              {c.hints.map((h) => <div key={h} className="warn-line">{h}</div>)}
              {c.log_tail && <details open={c.state !== 'ready' && c.state !== 'disabled'}><summary>What it printed</summary><pre>{visible(c.log_tail)}</pre></details>}
            </div>)}
            {scans[s.name] && <ScanPanel scan={scans[s.name]} busy={busy === s.name} onApprove={(tools) => void approve(s.name, tools)} />}
          </div>
        )
      })}
      {status?.error && <div className="warn-line">{status.error}</div>}
      {dialog === 'add' && <AddWizard onDone={() => void reload()} onClose={() => setDialog(null)} />}
      {dialog === 'manual' && <EditDialog server={null} onDone={() => void reload()} onClose={() => setDialog(null)} />}
      {dialog && typeof dialog === 'object' && <EditDialog server={dialog} onDone={() => void reload()} onClose={() => setDialog(null)} />}
    </Modal>
  )
}
