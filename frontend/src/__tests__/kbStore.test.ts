import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { POLL_MS } from '../kb'
import { useKb } from '../kbStore'
import type { Job } from '../types'

vi.mock('../api')
const m = vi.mocked(api)
const file = (size: number) => ({ size, name: 'f' }) as File
const items = (n: number, size = 1) => Array.from({ length: n }, (_, i) => ({ file: file(size), path: `f${i}` }))
const job = (state: Job['state'], updated_at: string): Job => ({
  id: 'j', kind: 'ingest', state, payload: {}, checkpoint: {}, attempts: 0, error: null, progress_total: 0, progress_done: 0,
  stats: {}, created_at: 't', updated_at, started_at: null, finished_at: null,
})
const ok = { accepted: 1, rejected: [], files: 1, bytes: 1 }

beforeEach(() => {
  vi.resetAllMocks()
  useKb.getState().stopPolling()
  useKb.setState({ activeId: 'c1', error: null, uploading: null, jobs: [], docFilter: '' })
  m.kbHealth.mockResolvedValue({ worker_alive: true, vector_backend: 'memory' })
  m.listJobs.mockResolvedValue([])
  m.kbStatus.mockResolvedValue({} as never)
  m.listDocuments.mockResolvedValue({ total: 0, items: [] })
  m.startUpload.mockResolvedValue({ upload_id: 'up1' })
  m.uploadFiles.mockResolvedValue(ok)
  m.commitUpload.mockResolvedValue(job('queued', 'u'))
  m.abandonUpload.mockResolvedValue(undefined)
})

describe('knowledge store', () => {
  it('uploadAll sends batches in order, then commits once', async () => {
    await useKb.getState().uploadAll(items(120))
    expect(m.uploadFiles.mock.calls.map((c) => [c[0], c[1].length])).toEqual([['up1', 50], ['up1', 50], ['up1', 20]])
    expect(m.commitUpload).toHaveBeenCalledTimes(1)
    expect(useKb.getState().uploading).toBeNull()
  })
  it('uploadAll reports rejected files and still commits', async () => {
    m.uploadFiles.mockResolvedValue({ ...ok, rejected: [{ path: '../x', reason: 'unsafe path' }] })
    await useKb.getState().uploadAll(items(2))
    expect(useKb.getState().error).toContain('../x')
    expect(m.commitUpload).toHaveBeenCalled()
  })
  it('uploadAll abandons the upload when a batch fails', async () => {
    m.uploadFiles.mockRejectedValue(new Error('boom'))
    await useKb.getState().uploadAll(items(2))
    expect(m.abandonUpload).toHaveBeenCalledWith('up1')
    expect(m.commitUpload).not.toHaveBeenCalled()
    expect(useKb.getState().error).toBe('boom')
  })
  it('uploadAll does not send files above the cap', async () => {
    await useKb.getState().uploadAll([{ file: file(60 * 1024 * 1024), path: 'big.bin' }, ...items(1)])
    expect(m.uploadFiles.mock.calls[0][1].map((i) => i.path)).toEqual(['f0'])
    expect(m.uploadFiles.mock.calls[0][2]).toEqual(['big.bin'])
  })
  it('the timer always polls health and jobs, and status only while a job is active or has changed', async () => {
    vi.useFakeTimers()
    try {
      m.listJobs.mockResolvedValueOnce([job('running', 'u1')]).mockResolvedValue([job('done', 'u2')])
      useKb.getState().startPolling()
      await vi.advanceTimersByTimeAsync(4 * POLL_MS)
      expect(m.kbHealth).toHaveBeenCalledTimes(4)
      expect(m.listJobs).toHaveBeenCalledTimes(4)
      expect(m.kbStatus).toHaveBeenCalledTimes(2)
      useKb.getState().stopPolling()
      await vi.advanceTimersByTimeAsync(4 * POLL_MS)
      expect(m.kbHealth).toHaveBeenCalledTimes(4)
    } finally { vi.useRealTimers() }
  })
  it('an action refreshes at once', async () => {
    m.ingestFolder.mockResolvedValue(job('queued', 'u'))
    await useKb.getState().ingestFolder('/x')
    expect(m.kbStatus).toHaveBeenCalledTimes(1)
    expect(m.listJobs).toHaveBeenCalledTimes(1)
  })
})

describe('an upload where every file is too large', () => {
  it('starts nothing and says why', async () => {
    await useKb.getState().uploadAll([{ file: file(60 * 1024 * 1024), path: 'big.bin' }])
    expect(m.startUpload).not.toHaveBeenCalled()
    expect(useKb.getState().error).toContain('over 50 MB')
  })
})
