import { describe, expect, it } from 'vitest'
import { alreadyIngested, duplicateJobs, jobEmpty, jobRemovable, formatBytes, formatEta, formatRate, jobActions, jobProgress, jobSource, jobsKey, relPath, sourceLabel, uploadBatches } from '../kb'
import type { Job } from '../types'

const job = (p: Partial<Job>): Job => ({
  id: 'j', kind: 'ingest', state: 'queued', payload: {}, checkpoint: {}, attempts: 0, error: null, progress_total: 0, progress_done: 0,
  stats: {}, created_at: 't', updated_at: 'u', started_at: null, finished_at: null, ...p,
})

describe('knowledge helpers', () => {
  it('relPath prefers the folder-relative path', () => {
    expect(relPath({ name: 'a.md', webkitRelativePath: 'notes/sub/a.md' })).toBe('notes/sub/a.md')
    expect(relPath({ name: 'a.md', webkitRelativePath: '' })).toBe('a.md')
  })
  it('uploadBatches splits by count and by bytes, keeping order', () => {
    const f = (n: number, size: number) => ({ path: `f${n}`, file: { size } })
    expect(uploadBatches([f(1, 1), f(2, 1), f(3, 1)], 2, 100).map((b) => b.map((x) => x.path))).toEqual([['f1', 'f2'], ['f3']])
    expect(uploadBatches([f(1, 60), f(2, 60), f(3, 500), f(4, 10)], 50, 100).map((b) => b.length)).toEqual([1, 1, 1, 1])
    expect(uploadBatches([], 2, 100)).toEqual([])
  })
  it('jobActions follows the transition table', () => {
    expect((['queued', 'running', 'paused', 'failed', 'done', 'cancelled'] as const).map(jobActions)).toEqual(
      [['pause', 'cancel'], ['pause', 'cancel'], ['resume', 'cancel'], ['retry', 'cancel'], [], []])
  })
  it('jobsKey changes when a job changes', () => {
    const a = [job({ id: 'a' }), job({ id: 'b' })]
    expect(jobsKey(a)).toBe(jobsKey([job({ id: 'a' }), job({ id: 'b' })]))
    expect(jobsKey(a)).not.toBe(jobsKey([job({ id: 'a', updated_at: 'v' }), job({ id: 'b' })]))
    expect(jobsKey(a)).not.toBe(jobsKey([...a, job({ id: 'c' })]))
  })
  it('jobProgress', () => {
    expect(jobProgress(job({ state: 'running', checkpoint: { phase: 'discover' }, stats: { discovered: 42 } }))).toEqual({ percent: null, label: 'Discovering… 42 files' })
    expect(jobProgress(job({ state: 'running', checkpoint: { phase: 'process' }, progress_total: 8, progress_done: 3 }))).toEqual({ percent: 37, label: '3/8 files' })
  })
  it('says what a job is doing before its first file is known', () => {
    const git = { source: { kind: 'git', url: 'https://github.com/o/r.git' } }
    expect(jobProgress(job({ state: 'queued', payload: git })).label).toBe('Waiting its turn (one job runs at a time)')
    expect(jobProgress(job({ state: 'running', payload: git, checkpoint: { phase: 'discover' } })).label).toBe('Cloning the repository… connecting')
    expect(jobProgress(job({ state: 'running', payload: git, checkpoint: { phase: 'discover' }, stats: { fetched_bytes: 20 * 1024 ** 2 } })).label)
      .toBe('Cloning the repository… 20.0 MB downloaded so far')
    expect(jobProgress(job({ state: 'running', payload: git, checkpoint: { phase: 'discover' }, stats: { fetched_bytes: 9, discovered: 12 } })).label).toBe('Discovering… 12 files')
    expect(jobProgress(job({ state: 'running', payload: { source: { kind: 'web', url: 'https://a.test/' } }, checkpoint: { phase: 'discover' }, stats: { discovered: 3, fetched_bytes: 2048 } })).label)
      .toBe('Crawling the site… 3 pages, 2.0 KB downloaded')
  })
  it('counts the duplicate entries and knows which may be removed', () => {
    const web = (id: string, state: Job['state'], url = 'https://a.test/') => job({ id, state, payload: { source: { kind: 'web', url } } })
    expect(['done', 'cancelled', 'failed', 'running', 'queued', 'paused'].map((s) => jobRemovable(s as Job['state']))).toEqual([true, true, false, false, false, false])
    expect(duplicateJobs([web('4', 'queued'), web('3', 'done'), web('2', 'done', 'https://b.test/'), web('1', 'done'), web('0', 'cancelled')])).toBe(2)
    expect(duplicateJobs([web('2', 'cancelled'), web('1', 'done')])).toBe(0)
    expect(duplicateJobs([])).toBe(0)
    expect([alreadyIngested('Already ingested: x was added'), alreadyIngested('x is already in the queue')]).toEqual([true, false])
  })
  it('knows a finished job that brought nothing in', () => {
    expect(jobEmpty(job({ state: 'done', stats: { ok: 0, unchanged: 0, failed: 1 } }))).toBe(true)
    expect(jobEmpty(job({ state: 'done', stats: { ok: 3, failed: 1 } }))).toBe(false)
    expect(jobEmpty(job({ state: 'done', stats: { ok: 0, unchanged: 2, failed: 1 } }))).toBe(false)
    expect(jobEmpty(job({ state: 'done', stats: { ok: 0, failed: 0 } }))).toBe(false)          // an empty folder is not a failure
    expect(jobEmpty(job({ state: 'failed', stats: { failed: 1 } }))).toBe(false)               // that one has Retry
    expect(jobEmpty(job({ kind: 'reembed', state: 'done', stats: { failed: 1 } }))).toBe(false)
  })
  it('formatEta and formatRate', () => {
    expect([null, 30, 720, 11100, 187200].map(formatEta)).toEqual(['—', '<1m', '12m', '3h 05m', '2d 4h'])
    expect([formatRate(4.5), formatRate(null)]).toEqual(['4.5 chunks/s', '—'])
  })
  it('formatBytes, jobSource, sourceLabel', () => {
    expect([512, 1536, 20 * 1024 ** 2, 2 * 1024 ** 3].map(formatBytes)).toEqual(['512 B', '1.5 KB', '20.0 MB', '2.00 GB'])
    expect(jobSource(job({ payload: { source: { kind: 'folder', path: '/x/y' } } }))).toBe('/x/y')
    expect(jobSource(job({ payload: { source: { kind: 'upload' } } }))).toBe('Browser upload')
    expect(jobSource(job({ kind: 'reembed', payload: { embedder: 'ollama:nomic-embed-text' } as never }))).toBe('Re-embed with ollama:nomic-embed-text')
    expect(jobProgress(job({ kind: 'reembed', progress_total: 10, progress_done: 4 })).label).toBe('4/10 chunks')
    expect(jobSource(job({ payload: { source: { kind: 'git', url: 'https://github.com/o/r.git' } } }))).toBe('Repository https://github.com/o/r.git')
    expect(jobSource(job({ payload: { source: { kind: 'web', url: 'https://acme.test/' } } }))).toBe('Website https://acme.test/')
    expect(sourceLabel('upload:notes/a.md', 3, 9)).toBe('upload:notes/a.md : lines 3–9')
  })
})
