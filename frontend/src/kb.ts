import type { Job, JobAction, JobState } from './types'

export const POLL_MS = 2000
export const BATCH_FILES = 50
export const BATCH_BYTES = 64 * 1024 * 1024
export const MAX_FILE_BYTES = 50 * 1024 * 1024   // mirror of the backend cap

/** What the bar above the message box says of a chat that reads no collection although there are some: the agent
 * is not offered kb_search there, and nothing else would tell the owner why it does not search. Null otherwise. */
export function noKnowledgeLine(attached: number, collections: number): string | null {
  if (attached > 0 || collections === 0) return null
  return 'This chat reads no collection, so the agent cannot search your knowledge base here. Attach one with '
    + '+ Knowledge; tick "every chat" beside it to have it in every chat that has no collections of its own.'
}

export const EVERY_CHAT_TITLE = 'Every chat reads this collection unless you chose its collections yourself: the chats you '
  + 'start from now on, and the ones that exist and have none of their own.'
export const FOLLOWS_TITLE = 'Nobody chose collections for this chat: it reads the ones ticked "every chat". Take one '
  + 'off here, and what is left is this chat\'s own choice.'

/** The path a file keeps inside the collection: its folder-relative path when it has one. */
export function relPath(f: { name: string; webkitRelativePath?: string }): string {
  return f.webkitRelativePath || f.name
}

/** Batches in order; one closes when the next item would pass either limit. */
export function uploadBatches<T extends { file: { size: number } }>(items: T[], maxFiles = BATCH_FILES, maxBytes = BATCH_BYTES): T[][] {
  const out: T[][] = []
  let cur: T[] = []
  let bytes = 0
  for (const it of items) {
    if (cur.length && (cur.length >= maxFiles || bytes + it.file.size > maxBytes)) { out.push(cur); cur = []; bytes = 0 }
    cur.push(it)
    bytes += it.file.size
  }
  if (cur.length) out.push(cur)
  return out
}

export function jobActions(state: JobState): JobAction[] {
  if (state === 'queued' || state === 'running') return ['pause', 'cancel']
  if (state === 'paused') return ['resume', 'cancel']
  if (state === 'failed') return ['retry', 'cancel']
  return []
}

export const isActive = (job: Job): boolean => job.state === 'queued' || job.state === 'running'

/** Two answers with the same key show the same queue. */
export const jobsKey = (jobs: Job[]): string => jobs.map((j) => `${j.id}@${j.updated_at}`).join('|')

export function jobProgress(job: Job): { percent: number | null; label: string } {
  const kind = job.payload.source?.kind
  // Nothing has started it yet: one job runs at a time.
  if (job.state === 'queued' && !job.checkpoint.phase && !job.progress_total) return { percent: null, label: 'Waiting its turn (one job runs at a time)' }
  if (job.checkpoint.phase === 'discover') {
    const found = job.stats.discovered ?? 0
    const fetched = job.stats.fetched_bytes ?? 0
    if (kind === 'git' && !found) return { percent: null, label: fetched ? `Cloning the repository… ${formatBytes(fetched)} downloaded so far` : 'Cloning the repository… connecting' }
    if (kind === 'web') return { percent: null, label: `Crawling the site… ${found} pages, ${formatBytes(fetched)} downloaded` }
    return { percent: null, label: `Discovering… ${found} files` }
  }
  return {
    percent: job.progress_total ? Math.floor((100 * job.progress_done) / job.progress_total) : null,
    label: `${job.progress_done}/${job.progress_total} ${job.kind === 'reembed' ? 'chunks' : 'files'}`,
  }
}

export function formatEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return '<1m'
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/** What a running job says while it waits for the machine to cool, with the part it waits for. Null when it does
 * not wait. A server from before it read the graphics card names no part: it waited for the processor. */
export function coolingNote(stats: Job['stats']): string | null {
  const [part, degrees] = typeof stats.cooling_gpu === 'number' ? ['graphics card', stats.cooling_gpu] : ['processor', stats.cooling]
  return typeof degrees === 'number' ? `cooling down: the ${part} is at ${Math.round(degrees)}°C, embedding goes on when it has dropped` : null
}

export const formatRate = (rate: number | null | undefined): string => (rate ? `${rate} chunks/s` : '—')

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

export function jobSource(job: Job): string {
  const s = job.payload.source
  if (job.kind === 'reembed') return `Re-embed with ${(job.payload as { embedder?: string }).embedder ?? 'another embedder'}`
  if (s?.kind === 'folder') return s.path ?? 'Folder'
  if (s?.kind === 'git') return `Repository ${s.url ?? ''}`.trim()
  if (s?.kind === 'web') return `Website ${s.url ?? ''}`.trim()
  return 'Browser upload'
}

export const sourceLabel = (uri: string, lineStart: number, lineEnd: number): string => `${uri} : lines ${lineStart}–${lineEnd}`

/** Where a passage stands in its document, as the model was shown it: "part 3 of 12 · in: Guide > Install", or "". */
export const sourcePlace = (s: { ordinal: number; parts?: number | null; where?: string }): string =>
  [s.parts && s.parts > 1 ? `part ${s.ordinal + 1} of ${s.parts}` : '', s.where ? `in: ${s.where}` : ''].filter(Boolean).join(' · ')

/** What a passage is called that leads the list because one half of the search had it first, and why it leads. */
export const FIRST_OF: Record<'vectors' | 'keywords', { label: string; title: string }> = {
  vectors: { label: 'nearest in meaning',
    title: 'Of all the passages the search weighed, this one is nearest to the question in meaning. It was put first for that.' },
  keywords: { label: 'best match in words',
    title: 'Of all the passages the search weighed, this one matches the words of the question best. It was put first for that, behind the nearest in meaning.' },
}

/** A finished entry may be removed from the list; what it ingested stays. */
export const jobRemovable = (state: JobState): boolean => state === 'done' || state === 'cancelled'

/** How many entries "remove duplicates" would take away: older finished runs of a source that has a newer finished one. */
export function duplicateJobs(jobs: Job[]): number {
  const kept = new Set<string>()
  let n = 0
  for (const j of jobs) {                       // the newest first, as the API lists them
    const s = j.payload.source
    const where = s?.url ?? s?.path ?? ''
    if (j.kind !== 'ingest' || !where || !jobRemovable(j.state)) continue
    const key = `${s?.kind}\n${where}`
    if (kept.has(key)) n += 1
    else if (j.state === 'done') kept.add(key)
  }
  return n
}

/** The API's refusal of a source the collection already has starts with these words. */
export const alreadyIngested = (message: string): boolean => message.startsWith('Already ingested')

/** A finished ingest that brought nothing in (its source could not be fetched): worth trying again, not a record to keep. */
export const jobEmpty = (job: Job): boolean =>
  job.kind === 'ingest' && job.state === 'done' && !(job.stats.ok ?? 0) && !(job.stats.unchanged ?? 0) && (job.stats.failed ?? 0) > 0
