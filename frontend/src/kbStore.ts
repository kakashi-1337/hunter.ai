import { create } from 'zustand'
import * as api from './api'
import { alreadyIngested, isActive, jobsKey, MAX_FILE_BYTES, POLL_MS, uploadBatches } from './kb'
import type { Collection, Job, JobAction, KbDocument, KbHealth, KbHit, KbStatus } from './types'

interface KbState {
  collections: Collection[]
  activeId: string | null
  status: KbStatus | null
  jobs: Job[]
  documents: { total: number; items: KbDocument[] }
  docFilter: string
  hits: KbHit[] | null
  health: KbHealth | null
  error: string | null
  uploading: { sent: number; total: number } | null
  loaded: boolean
  load(): Promise<void>
  select(id: string | null): Promise<void>
  create(name: string, embedder?: string): Promise<boolean>
  rename(id: string, name: string): Promise<void>
  remove(id: string): Promise<void>
  refresh(): Promise<void>
  tick(): Promise<void>
  startPolling(): void
  stopPolling(): void
  uploadAll(items: { file: File; path: string }[]): Promise<void>
  ingestFolder(path: string): Promise<void>
  ingestGit(url: string): Promise<void>
  removeJob(jobId: string): Promise<void>
  tryAgain(job: Job): Promise<void>
  pruneJobs(): Promise<void>
  addNote(title: string, text: string): Promise<boolean>
  reembed(embedder: string): Promise<void>
  ingestWeb(body: Parameters<typeof api.ingestWeb>[1]): Promise<void>
  act(jobId: string, action: JobAction): Promise<void>
  setFilter(status: string): Promise<void>
  loadMore(): Promise<void>
  search(query: string, limit: number): Promise<void>
  setError(message: string | null): void
}

const text = (e: unknown) => (e instanceof Error ? e.message : String(e))
const text_ = text
let timer: ReturnType<typeof setInterval> | null = null
let lastKey = ''

export const useKb = create<KbState>((set, get) => {
  const guard = async (fn: () => Promise<void>) => {
    try { await fn() } catch (e) { set({ error: text(e) }) }
  }
  /** Queue a source; when the collection already has it, say so and queue it again only if the owner wants a refresh. */
  const unlessHad = async (send: (again: boolean) => Promise<unknown>) => {
    try { await send(false) } catch (e) {
      if (!alreadyIngested(text(e))) throw e
      if (window.confirm(`${text(e)}\n\nIngest it again now?`)) await send(true)
    }
  }
  /** Status and the first page of documents for the collection that is still the active one. */
  const details = async (id: string) => {
    const [status, documents] = await Promise.all([
      api.kbStatus(id), api.listDocuments(id, { status: get().docFilter || undefined, limit: 100 })])
    if (get().activeId === id) set({ status, documents })
  }
  return {
    collections: [], activeId: null, status: null, jobs: [], documents: { total: 0, items: [] }, docFilter: '',
    hits: null, health: null, error: null, uploading: null, loaded: false,

    load: () => guard(async () => {
      const [collections, health] = await Promise.all([api.listCollections(), api.kbHealth()])
      set({ collections, health, loaded: true })
      const keep = get().activeId && collections.some((c) => c.id === get().activeId)
      await get().select(keep ? get().activeId : collections[0]?.id ?? null)
    }),

    select: async (id) => {
      set({ activeId: id, status: null, jobs: [], documents: { total: 0, items: [] }, hits: null, docFilter: '' })
      lastKey = ''
      if (id) await get().refresh()
    },

    create: async (name, embedder) => {
      try {
        const col = await api.createCollection(name, embedder)
        set({ collections: await api.listCollections() })
        await get().select(col.id)
        return true
      } catch (e) { set({ error: text(e) }); return false }
    },

    rename: (id, name) => guard(async () => {
      await api.renameCollection(id, name)
      set({ collections: await api.listCollections() })
      if (get().activeId === id) await get().refresh()
    }),

    remove: (id) => guard(async () => {
      await api.deleteCollection(id)
      const collections = await api.listCollections()
      set({ collections })
      if (get().activeId === id) await get().select(collections[0]?.id ?? null)
    }),

    refresh: () => guard(async () => {
      const id = get().activeId
      const health = await api.kbHealth()
      set({ health })
      if (!id) return
      const jobs = await api.listJobs(id)
      if (get().activeId !== id) return       // a late answer for the previous collection
      lastKey = jobsKey(jobs)
      set({ jobs })
      await details(id)
    }),

    tick: async () => {
      try {
        const id = get().activeId
        set({ health: await api.kbHealth() })
        if (!id) return
        const jobs = await api.listJobs(id)
        if (get().activeId !== id) return
        const key = jobsKey(jobs)
        const changed = key !== lastKey
        lastKey = key
        set({ jobs })
        if (changed || jobs.some(isActive)) await details(id)
      } catch { /* the next tick tries again; a banner every two seconds helps nobody */ }
    },

    startPolling: () => { if (!timer) timer = setInterval(() => void get().tick(), POLL_MS) },
    stopPolling: () => { if (timer) { clearInterval(timer); timer = null } },

    uploadAll: async (items) => {
      const id = get().activeId
      if (!id || !items.length) return
      const tooLarge = items.filter((i) => i.file.size > MAX_FILE_BYTES).map((i) => i.path)
      const batches = uploadBatches(items.filter((i) => i.file.size <= MAX_FILE_BYTES))
      if (!batches.length) {       // nothing would be accepted, so there is no job to hold a history
        set({ error: `Nothing was sent: ${tooLarge.length === 1 ? 'the file is' : `all ${tooLarge.length} files are`} over 50 MB.` })
        return
      }
      let uploadId: string | null = null
      const rejected: string[] = []
      set({ uploading: { sent: 0, total: items.length - tooLarge.length }, error: null })
      try {
        uploadId = (await api.startUpload(id)).upload_id
        let sent = 0
        const rounds = batches.length ? batches : [[]]
        for (let n = 0; n < rounds.length; n++) {
          const res = await api.uploadFiles(uploadId, rounds[n], n === 0 ? tooLarge : [])
          rejected.push(...res.rejected.map((r) => `${r.path} (${r.reason})`))
          sent += rounds[n].length
          set({ uploading: { sent, total: items.length - tooLarge.length } })
        }
        await api.commitUpload(uploadId)
        if (rejected.length) set({ error: `Not taken: ${rejected.slice(0, 5).join('; ')}${rejected.length > 5 ? ` and ${rejected.length - 5} more` : ''}. They are listed in the history.` })
      } catch (e) {
        if (uploadId) await api.abandonUpload(uploadId).catch(() => {})
        set({ error: text(e) })
      } finally {
        set({ uploading: null })
      }
      await get().refresh()
    },

    ingestFolder: (path) => guard(async () => { const id = get().activeId; if (id) { await unlessHad((again) => api.ingestFolder(id, path, again)); await get().refresh() } }),
    ingestGit: (url) => guard(async () => { const id = get().activeId; if (id) { await unlessHad((again) => api.ingestGit(id, url, again)); await get().refresh() } }),
    ingestWeb: (body) => guard(async () => { const id = get().activeId; if (id) { await unlessHad((again) => api.ingestWeb(id, { ...body, again })); await get().refresh() } }),
    tryAgain: (job) => guard(async () => {
      // The same source once more, as a new job; the entry that brought nothing in goes.
      const id = get().activeId
      const src = job.payload.source as (Record<string, unknown> & { kind?: string; url?: string; path?: string }) | undefined
      if (!id || !src) return
      if (src.kind === 'git' && src.url) await api.ingestGit(id, src.url, true)
      else if (src.kind === 'web' && src.url) await api.ingestWeb(id, { url: src.url, max_pages: Number(src.max_pages ?? 200), max_depth: Number(src.max_depth ?? 2),
        robots: src.robots !== false, allow_local: src.allow_local === true, render: src.render === true, again: true })
      else if (src.kind === 'folder' && src.path) await api.ingestFolder(id, src.path, true)
      else return
      await api.deleteJob(job.id)
      await get().refresh()
    }),
    removeJob: (jobId) => guard(async () => { await api.deleteJob(jobId); await get().refresh() }),
    pruneJobs: () => guard(async () => { const id = get().activeId; if (id) { await api.pruneJobs(id); await get().refresh() } }),
    addNote: async (title, text) => {
      const id = get().activeId
      if (!id) return false
      try { await api.addNote(id, title, text); await get().refresh(); return true } catch (e) { set({ error: text_(e) }); return false }
    },
    reembed: (embedder) => guard(async () => { const id = get().activeId; if (id) { await api.reembed(id, embedder); set({ collections: await api.listCollections() }); await get().refresh() } }),
    act: (jobId, action) => guard(async () => { await api.jobAction(jobId, action); await get().refresh() }),
    setFilter: async (status) => { set({ docFilter: status }); const id = get().activeId; if (id) await guard(() => details(id)) },
    loadMore: () => guard(async () => {
      const id = get().activeId
      if (!id) return
      const cur = get().documents
      const next = await api.listDocuments(id, { status: get().docFilter || undefined, limit: 100, offset: cur.items.length })
      if (get().activeId === id) set({ documents: { total: next.total, items: [...cur.items, ...next.items] } })
    }),
    search: (query, limit) => guard(async () => {
      const id = get().activeId
      if (id) set({ hits: (await api.kbSearch([id], query, limit)).hits })
    }),
    setError: (message) => set({ error: message }),
  }
})
