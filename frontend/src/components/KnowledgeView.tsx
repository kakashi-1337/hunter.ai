import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type { KbChunk, KbDocument } from '../types'
import { FIRST_OF, duplicateJobs, jobEmpty, formatBytes, formatEta, formatRate, jobActions, jobProgress, jobRemovable, jobSource, relPath, sourceLabel } from '../kb'
import { useKb } from '../kbStore'
import { visible } from '../tree'
import { confirmBox, promptBox } from '../dialog'
import { Modal } from './Modal'

const DEFAULT_EMBEDDER = 'ollama:bge-m3'

function CreateDialog({ onClose }: { onClose: () => void }) {
  const create = useKb((s) => s.create)
  const [name, setName] = useState('')
  const [embedder, setEmbedder] = useState(DEFAULT_EMBEDDER)
  const [busy, setBusy] = useState(false)
  const go = async () => { setBusy(true); const ok = await create(name, embedder.trim() || undefined); setBusy(false); if (ok) onClose() }
  return (
    <Modal title="New collection" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name.trim() || busy} onClick={go}>{busy ? 'Creating…' : 'Create'}</button></>}>
      <label className="field"><span>Name</span><input autoFocus value={name} maxLength={80} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && name.trim() && void go()} /></label>
      <label className="field"><span>Embedder (a local Ollama embedding model; its dimension is read from the model and fixed for this collection)</span>
        <input className="mono" value={embedder} onChange={(e) => setEmbedder(e.target.value)} /></label>
    </Modal>
  )
}

function Sources() {
  const { uploading, uploadAll, ingestFolder, ingestGit, ingestWeb } = useKb()
  const [tab, setTab] = useState<'files' | 'folder' | 'web' | 'repo'>('files')
  const [url, setUrl] = useState('')
  const [repo, setRepo] = useState('')
  const [pages, setPages] = useState(200)
  const [depth, setDepth] = useState(2)
  const [robots, setRobots] = useState(true)
  const [render, setRender] = useState(false)
  const [local, setLocal] = useState(false)
  const [picked, setPicked] = useState<{ file: File; path: string }[]>([])
  const [path, setPath] = useState('')
  const dir = useRef<HTMLInputElement>(null)
  useEffect(() => { dir.current?.setAttribute('webkitdirectory', '') }, [tab])
  const take = (list: FileList | null) => setPicked(Array.from(list ?? []).map((file) => ({ file, path: relPath(file) })))
  const total = picked.reduce((n, p) => n + p.file.size, 0)
  return (
    <section className="kb-card">
      <div className="seg" role="tablist">
        <button className={tab === 'files' ? 'on' : ''} onClick={() => setTab('files')}>Files</button>
        <button className={tab === 'folder' ? 'on' : ''} onClick={() => setTab('folder')}>Folder</button>
        <button className={tab === 'web' ? 'on' : ''} onClick={() => setTab('web')}>Web</button>
        <button className={tab === 'repo' ? 'on' : ''} onClick={() => setTab('repo')}>Repo</button>
      </div>
      {tab === 'files' ? (
        <div className="kb-drop" onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); take(e.dataTransfer.files) }}>
          <div className="row">
            <label className="btn">Choose files<input type="file" multiple hidden onChange={(e) => take(e.target.files)} /></label>
            <label className="btn">Choose folder<input ref={dir} type="file" multiple hidden onChange={(e) => take(e.target.files)} /></label>
            <span className="muted grow">{picked.length ? `${picked.length} files · ${formatBytes(total)}` : 'or drop files here'}</span>
            <button className="btn primary" disabled={!picked.length || uploading !== null} onClick={async () => { await uploadAll(picked); setPicked([]) }}>
              {uploading ? `${uploading.sent}/${uploading.total} files` : 'Ingest'}</button>
          </div>
          <div className="hint">Up to 50 MB per file, 2 GB and 50,000 files per job. zip, tar, gz, xz, and 7z are unpacked. Text, Markdown, code, CSV, SQL, HTML, PDF, Office and OpenDocument files are read, and pictures and scanned pages by OCR; anything else is listed as skipped.</div>
        </div>
      ) : tab === 'web' ? (
        <div>
          <div className="row">
            <input className="grow mono" placeholder="https://docs.example.com/" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="btn primary" disabled={!/^https?:\/\/\S+$/.test(url.trim())}
              onClick={() => { void ingestWeb({ url: url.trim(), max_pages: pages, max_depth: depth, robots, allow_local: local, render }); setLocal(false) }}>Crawl and ingest</button>
          </div>
          <div className="row kb-opts">
            <label>Pages <input type="number" min={1} max={5000} value={pages} onChange={(e) => setPages(Math.max(1, Math.min(5000, Number(e.target.value) || 200)))} /></label>
            <label>Depth <input type="number" min={0} max={10} value={depth} onChange={(e) => setDepth(Math.max(0, Math.min(10, Number(e.target.value) || 0)))} /></label>
            <label className="check"><input type="checkbox" checked={robots} onChange={(e) => setRobots(e.target.checked)} /><span>Respect robots.txt</span></label>
            <label className="check"><input type="checkbox" checked={render} onChange={(e) => setRender(e.target.checked)} /><span>Open pages that need JavaScript in the browser (slower)</span></label>
            <label className="check"><input type="checkbox" checked={local} onChange={(e) => setLocal(e.target.checked)} /><span>This site is on my machine or network</span></label>
          </div>
          <div className="hint">Stays on the host of the URL. The URL may be an RSS or Atom feed: its posts on that host are ingested. HTML, text, Markdown, PDF, JSON, CSV, and XML pages are kept, up to 5 MB each, one request every half second. Crawl only sites you are allowed to.</div>
        </div>
      ) : tab === 'repo' ? (
        <div>
          <div className="row">
            <input className="grow mono" placeholder="https://github.com/owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
            <button className="btn primary" disabled={!/^https:\/\/(github|gitlab)\.com\/\S+\/\S+$/.test(repo.trim())} onClick={() => void ingestGit(repo.trim())}>Clone and ingest</button>
          </div>
          <div className="hint">A shallow clone of a public GitHub or GitLab repository (latest commit only, no submodules, nothing in it runs). The same files are skipped as in a folder scan.</div>
        </div>
      ) : (
        <div>
          <div className="row">
            <input className="grow mono" placeholder="/absolute/path/on/this/machine" value={path} onChange={(e) => setPath(e.target.value)} />
            <button className="btn primary" disabled={!path.trim().startsWith('/')} onClick={() => void ingestFolder(path.trim())}>Scan and ingest</button>
          </div>
          <div className="hint">Read in place, nothing is copied. Skipped: .git, node_modules, __pycache__, virtual environments, symlinks, files over 50 MB, and secret-like names (.env, keys).</div>
        </div>
      )}
    </section>
  )
}

function Queue() {
  const { jobs, act, removeJob, pruneJobs, tryAgain } = useKb()
  const duplicates = duplicateJobs(jobs)
  return (
    <section className="kb-card">
      <div className="row"><h3 className="section grow">Ingest queue</h3>
        {duplicates > 0 && <button className="btn small" title="Older finished runs of a source that was ingested again. Only the entries go: what was ingested stays."
          onClick={() => void pruneJobs()}>Remove {duplicates} duplicate {duplicates === 1 ? 'entry' : 'entries'}</button>}</div>
      {jobs.length === 0 && <div className="hint">No ingest jobs yet.</div>}
      {jobs.map((j) => {
        const p = jobProgress(j)
        const s = j.stats
        return (
          <div key={j.id} className="kb-job">
            <div className="row">
              <span className={`badge ${j.state === 'failed' || jobEmpty(j) ? 'bad' : j.state === 'paused' ? 'warn' : ''}`}>{jobEmpty(j) ? 'nothing ingested' : j.state}</span>
              <span className="mono grow kb-ellipsis" title={jobSource(j)}>{visible(jobSource(j))}</span>
              {jobActions(j.state).map((a) => <button key={a} className="btn small" onClick={() => void act(j.id, a)}>{a}</button>)}
              {jobEmpty(j) && j.payload.source?.kind !== 'upload' && <button className="btn small" title="Fetch this source again as a new job" onClick={() => void tryAgain(j)}>try again</button>}
              {jobRemovable(j.state) && <button className="icon" aria-label="Remove this entry" title="Remove this entry from the list (what it ingested stays)" onClick={() => void removeJob(j.id)}>✕</button>}
            </div>
            <div className="kb-progress"><div style={{ width: `${p.percent ?? (j.state === 'done' ? 100 : 0)}%` }} /></div>
            <div className="hint">
              {p.label} · ok {s.ok ?? 0} · unchanged {s.unchanged ?? 0} · skipped {s.skipped ?? 0} · failed {s.failed ?? 0}
              {j.state === 'running' && <> · {formatRate(s.embed_rate)} · ETA {formatEta(s.eta_seconds)}</>}
              {j.state === 'running' && typeof s.cooling === 'number' && <span className="warn-line"> · cooling down: the processor is at {Math.round(s.cooling)}°C, embedding goes on when it has dropped</span>}
            </div>
            {jobEmpty(j) && <div className="warn-line">The source could not be fetched, so nothing was ingested. The reason is on its entry under Documents (filter: failed).</div>}
            {j.state === 'failed' && <div className="warn-line">{j.error} <span className="hint">Staged files are kept for Retry. Cancel frees the space; after 7 days the job is cancelled automatically.</span></div>}
          </div>
        )
      })}
    </section>
  )
}

/** One document's chunks: read, correct, or remove them. Text is plain text throughout. */
function DocumentDialog({ doc, onClose }: { doc: KbDocument; onClose: () => void }) {
  const { refresh, setError } = useKb()
  const [chunks, setChunks] = useState<KbChunk[]>([])
  const [total, setTotal] = useState(0)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e))
  const load = async (offset = 0) => {
    try {
      const r = await api.listChunks(doc.id, offset)
      setTotal(r.total)
      setChunks((cur) => (offset ? [...cur, ...r.items] : r.items))
    } catch (e) { fail(e) }
  }
  useEffect(() => { void load() }, [doc.id]) // eslint-disable-line react-hooks/exhaustive-deps
  const run = async (fn: () => Promise<void>) => { setBusy(true); try { await fn() } catch (e) { fail(e) } finally { setBusy(false) } }
  const save = (c: KbChunk) => run(async () => {
    const next = await api.editChunk(c.id, draft)
    setChunks((cur) => cur.map((x) => (x.id === c.id ? next : x)))
    setEditing(null)
  })
  const drop = (c: KbChunk) => run(async () => {
    if (!(await confirmBox('Delete this chunk from the index?'))) return
    await api.deleteChunk(c.id)
    setChunks((cur) => cur.filter((x) => x.id !== c.id)); setTotal((n) => n - 1)
    await refresh()
  })
  const dropAll = () => run(async () => {
    if (!(await confirmBox('Remove this document and all its chunks from the index? The original file is not touched.'))) return
    await api.deleteDocument(doc.id)
    await refresh(); onClose()
  })
  return (
    <Modal title="Document" onClose={onClose} footer={<>
      <button className="btn danger" disabled={busy} onClick={dropAll}>Remove from index</button>
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      <div className="mono kb-wrap">{visible(doc.source_uri)}</div>
      <div className="hint">{doc.status}{doc.reason ? ` · ${doc.reason}` : ''} · {total} chunks · {formatBytes(doc.size)}</div>
      {chunks.map((c) => (
        <div key={c.id} className="kb-hit">
          <div className="row"><b>{c.ordinal}</b><span className="muted grow">lines {c.line_start}–{c.line_end}</span>
            {editing === c.id ? (<>
              <button className="btn small" onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn small primary" disabled={busy || !draft.trim() || draft.length > 2000} onClick={() => void save(c)}>Save and re-embed</button></>
            ) : (<>
              <button className="btn small" disabled={busy} onClick={() => { setEditing(c.id); setDraft(c.text) }}>Edit</button>
              <button className="btn small" disabled={busy} onClick={() => void drop(c)}>Delete</button></>)}
          </div>
          {editing === c.id
            ? <><textarea rows={8} className="mono" value={draft} onChange={(e) => setDraft(e.target.value)} /><div className="hint">{draft.length}/2000 characters</div></>
            : <pre>{visible(c.text)}</pre>}
        </div>
      ))}
      {chunks.length < total && <button className="btn" onClick={() => void load(chunks.length)}>Load more ({total - chunks.length} left)</button>}
    </Modal>
  )
}

function NoteDialog({ onClose }: { onClose: () => void }) {
  const addNote = useKb((s) => s.addNote)
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const go = async () => { setBusy(true); const ok = await addNote(title, text); setBusy(false); if (ok) onClose() }
  return (
    <Modal title="Add a note" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={busy || !title.trim() || !text.trim()} onClick={go}>{busy ? 'Embedding…' : 'Add to the index'}</button></>}>
      <label className="field"><span>Title (a note with the same title is replaced)</span><input autoFocus value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="field"><span>Text</span><textarea rows={12} value={text} onChange={(e) => setText(e.target.value)} /></label>
    </Modal>
  )
}

function History() {
  const { documents, docFilter, setFilter, loadMore, status } = useKb()
  const [open, setOpen] = useState<KbDocument | null>(null)
  const [note, setNote] = useState(false)
  const counts = status?.documents
  const filters: [string, string][] = [['', 'All'], ['ok', `OK${counts ? ` (${counts.ok})` : ''}`], ['skipped', `Skipped${counts ? ` (${counts.skipped})` : ''}`], ['failed', `Failed${counts ? ` (${counts.failed})` : ''}`]]
  return (
    <section className="kb-card">
      <div className="row"><h3 className="section grow">Documents</h3>
        <button className="btn small" onClick={() => setNote(true)}>+ Note</button>
        <div className="seg">{filters.map(([v, label]) => <button key={v} className={docFilter === v ? 'on' : ''} onClick={() => void setFilter(v)}>{label}</button>)}</div>
      </div>
      {documents.items.length === 0 ? <div className="hint">Nothing here yet.</div> : (
        <table className="kb-table">
          <thead><tr><th>Source</th><th>Status</th><th>Reason</th><th>Chunks</th><th>Updated</th></tr></thead>
          <tbody>{documents.items.map((d) => (
            <tr key={d.id}><td className="mono"><button className="kb-link" title="Open its chunks" onClick={() => setOpen(d)}>{visible(d.source_uri)}</button></td><td><span className={`badge ${d.status === 'failed' ? 'bad' : d.status === 'skipped' ? 'warn' : ''}`}>{d.status}</span></td>
              <td>{d.reason ? visible(d.reason) : ''}</td><td>{d.chunk_count}</td><td className="muted">{d.updated_at.slice(0, 19).replace('T', ' ')}</td></tr>
          ))}</tbody>
        </table>
      )}
      {documents.items.length < documents.total && <button className="btn" onClick={() => void loadMore()}>Load more ({documents.total - documents.items.length} left)</button>}
      {open && <DocumentDialog doc={open} onClose={() => setOpen(null)} />}
      {note && <NoteDialog onClose={() => setNote(false)} />}
    </section>
  )
}

function SearchBox() {
  const { hits, search } = useKb()
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(8)
  return (
    <section className="kb-card">
      <h3 className="section">Search this collection</h3>
      <div className="row">
        <input className="grow" placeholder="Words or a question" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && query.trim() && void search(query, limit)} />
        <input type="number" min={1} max={50} value={limit} aria-label="Limit" style={{ width: 64 }} onChange={(e) => setLimit(Math.max(1, Math.min(50, Number(e.target.value) || 8)))} />
        <button className="btn" disabled={!query.trim()} onClick={() => void search(query, limit)}>Search</button>
      </div>
      {hits && hits.length === 0 && <div className="hint">No passage matched.</div>}
      {hits?.map((h, i) => (
        <div key={h.chunk_id} className="kb-hit">
          <div className="row"><b>{i + 1}</b>{!h.exact && !h.first_of && <span className="muted">{h.score.toFixed(4)}</span>}
            {h.exact && <span className="badge ok" title="This passage holds an identifier of the query letter for letter, and is put first for that.">exact spelling</span>}
            {!h.exact && h.first_of && <span className="badge ok" title={FIRST_OF[h.first_of].title}>{FIRST_OF[h.first_of].label}</span>}
            {h.dense_rank !== null && <span className="badge">dense #{h.dense_rank}</span>}
            {h.keyword_rank !== null && <span className="badge">keyword #{h.keyword_rank}</span>}
            <span className="mono grow kb-ellipsis">{visible(sourceLabel(h.source_uri, h.line_start, h.line_end))}</span><span className="muted">chunk {h.ordinal}</span></div>
          <pre>{visible(h.text)}</pre>
        </div>
      ))}
    </section>
  )
}

export function KnowledgeView({ onClose }: { onClose: () => void }) {
  const { collections, activeId, status, health, error, loaded } = useKb()
  const { load, select, rename, remove, reembed, startPolling, stopPolling, setError } = useKb()
  const [creating, setCreating] = useState(false)
  useEffect(() => { void load(); startPolling(); return stopPolling }, [load, startPolling, stopPolling])
  const active = collections.find((c) => c.id === activeId)
  const d = status?.documents
  const outOfSync = status && !status.active_job && status.vectors !== null && status.vectors !== status.chunks
  return (
    <main className="main kb">
      <header className="chat-head">
        <span className="title">Knowledge</span>
        <select aria-label="Collection" value={activeId ?? ''} onChange={(e) => void select(e.target.value || null)} disabled={!collections.length}>
          {!collections.length && <option value="">no collection yet</option>}
          {collections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <button className="btn primary" onClick={() => setCreating(true)}>+ Collection</button>
        {active && <button className="btn" onClick={() => void promptBox('New name', active.name).then((n) => { if (n?.trim()) void rename(active.id, n.trim()) })}>Rename</button>}
        {active && <button className="btn" title="Embed everything again with another model" onClick={() => void promptBox('Embed this whole collection again with which embedder? Search finds less until the job is done.', active.embedder).then((e) => { if (e?.trim() && e.trim() !== active.embedder) void reembed(e.trim()) })}>Re-embed</button>}
        {active && <button className="btn danger" onClick={() => void confirmBox(`Delete the collection "${active.name}" and everything indexed in it? The original files are not touched.`).then((ok) => { if (ok) void remove(active.id) })}>Delete</button>}
        <span className="grow" />
        <button className="btn" title="Back to the chat (Ctrl+Shift+K)" onClick={onClose}>Back to chat</button>
      </header>
      {error && <div className="banner" role="alert"><span className="grow">{error}</span><button className="icon" aria-label="Dismiss" onClick={() => setError(null)}>✕</button></div>}
      {health && !health.worker_alive && <div className="banner" role="status">The ingest worker is not running, so queued jobs wait. Start HunterAI with scripts/start.sh (or scripts/deploy.sh).</div>}
      <div className="kb-body">
        {!loaded ? <div className="muted">Loading…</div> : !active ? (
          <div className="welcome"><h1>Knowledge</h1><p>Collections hold your notes, write-ups, code, and documents so a chat can search them. Create one to begin.</p></div>
        ) : (
          <>
            <div className="kb-status">
              <span>Documents <b>{d ? d.ok : '—'}</b>{d && (d.skipped || d.failed || d.processing) ? <span className="muted"> (+{d.skipped} skipped, {d.failed} failed{d.processing ? `, ${d.processing} in progress` : ''})</span> : null}</span>
              <span>Chunks <b>{status?.chunks ?? '—'}</b></span>
              <span>Vectors <b>{status ? (status.vector_error ? <span className="warn-line">{status.vector_error}</span> : status.vectors) : '—'}</b></span>
              <span>Dimension <b>{active.dimension}</b></span>
              <span>Embedder <b className="mono">{active.embedder}</b></span>
              {status?.active_job?.state === 'running' && <><span>Rate <b>{formatRate(status.embed_rate)}</b></span><span>ETA <b>{formatEta(status.eta_seconds)}</b></span></>}
              {outOfSync && <span className="badge warn">index out of sync</span>}
              <span className="muted">store: {health?.vector_backend ?? '—'}</span>
            </div>
            <Sources /><Queue /><SearchBox /><History />
          </>
        )}
      </div>
      {creating && <CreateDialog onClose={() => setCreating(false)} />}
    </main>
  )
}
