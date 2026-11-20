// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { MessageItem } from '../components/MessageItem'
import { RunChanges } from '../components/RunChanges'
import { answer, useDialog } from '../dialog'
import { useStore } from '../store'
import { changesOf, changesView, renderItems, undoQuestion } from '../tree'
import type { Block, ChangesBlock, Message } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const message = (blocks: Block[]): Message => ({
  id: 'm1', chat_id: 'c1', parent_id: null, role: 'assistant', blocks, provider: 'ollama', model: 'qwen3:8b',
  fallback_from: null, status: 'complete', created_at: '2026-11-17T10:00:00Z', siblings: ['m1'],
})
const changes = (over: Partial<ChangesBlock> = {}): ChangesBlock => ({
  type: 'changes', run_id: 'r1', folder: '/home/dave/project', before: 'a'.repeat(40), after: 'b'.repeat(40), reason: '', undone: false, left: [],
  files: [{ path: 'README.md', change: 'deleted' }, { path: 'app.py', change: 'changed' }, { path: 'tests/test_app.py', change: 'new' }],
  ...over,
})
const NO_SAVE_POINT = 'No save point was kept for this run: git is not installed on this machine. What it changed cannot be listed or put back from here.'

describe('what a run changed, under its answer', () => {
  it('is found in the message and is not drawn as one of its parts', () => {
    const m = message([{ type: 'text', text: 'Fixed.' }, changes()])
    expect(changesOf(m)).toEqual(changes())
    expect(renderItems(m)).toEqual([{ kind: 'text', text: 'Fixed.' }])
    expect(changesOf(message([{ type: 'text', text: 'Nothing to do.' }]))).toBeNull()
  })

  it('says how many files, where, and that it can be undone', () => {
    expect(changesView(changes())).toEqual({ text: 'This run changed 3 files in /home/dave/project.', level: '', canUndo: true })
    expect(changesView(changes({ files: [{ path: 'app.py', change: 'changed' }] })).text).toBe('This run changed 1 file in /home/dave/project.')
  })

  it('says that the files were put back, and offers no second undo', () => {
    expect(changesView(changes({ undone: true }))).toEqual({
      text: 'This run changed 3 files in /home/dave/project. They were put back as they were before it.', level: '', canUndo: false })
  })

  it('names the files that were left because they changed after the run, and can be asked again', () => {
    expect(changesView(changes({ left: ['app.py'] }))).toEqual({
      text: 'This run changed 3 files in /home/dave/project. 1 of them changed after the run and was left as it is: app.py. The others are as they were before it.',
      level: 'warn', canUndo: true })
    expect(changesView(changes({ left: ['app.py', 'README.md'] })).text)
      .toBe('This run changed 3 files in /home/dave/project. 2 of them changed after the run and were left as they are: app.py, README.md. The other is as it was before it.')
  })

  it('does not say that a file was put back when it was left', () => {
    expect(changesView(changes({ files: [{ path: 'app.py', change: 'changed' }], left: ['app.py'] }))).toEqual({
      text: 'This run changed 1 file in /home/dave/project. It changed after the run and was left as it is.', level: 'warn', canUndo: true })
    expect(changesView(changes({ left: ['README.md', 'app.py', 'tests/test_app.py'] })).text)
      .toBe('This run changed 3 files in /home/dave/project. All of them changed after the run and were left as they are.')
    expect(changesView(changes({ files: [{ path: 'app.py', change: 'changed' }], undone: true })).text)
      .toBe('This run changed 1 file in /home/dave/project. It was put back as it was before it.')
  })

  it('says why there is no list when no save point was kept', () => {
    expect(changesView(changes({ files: [], before: '', after: '', reason: NO_SAVE_POINT }))).toEqual({ text: NO_SAVE_POINT, level: 'warn', canUndo: false })
  })

  it('asks before it puts anything back, and says what is left alone', () => {
    expect(undoQuestion(changes())).toBe(
      'Put the 3 files this run changed in /home/dave/project back as they were before it? A file that changed after the run is left as it is.')
    expect(undoQuestion(changes({ files: [{ path: 'app.py', change: 'changed' }] }))).toBe(
      'Put the 1 file this run changed in /home/dave/project back as it was before it? A file that changed after the run is left as it is.')
  })
})

describe('the list of changes and its undo button', () => {
  let root: Root
  let host: HTMLElement
  beforeEach(() => {
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    useStore.setState({ messages: [message([{ type: 'text', text: 'Fixed.' }, changes()])], live: null, error: null })
    useDialog.setState({ current: null, waiting: [] })
  })
  afterEach(() => {
    act(() => root.unmount())
    host.remove()
    vi.restoreAllMocks()
  })
  const shown = () => changesOf(useStore.getState().messages[0])!
  const draw = async () => { await act(async () => { root.render(createElement(RunChanges, { block: shown() })) }) }
  const button = () => [...host.querySelectorAll('button')].find((b) => /Undo these changes|Putting back/.test(b.textContent ?? ''))
  const click = async () => { await act(async () => { button()!.dispatchEvent(new MouseEvent('click', { bubbles: true })) }) }
  const rows = () => [...host.querySelectorAll('li')].map((li) => li.textContent ?? '')

  it('lists every file with what happened to it', async () => {
    await draw()
    expect(host.textContent).toContain('This run changed 3 files in /home/dave/project.')
    expect(rows()).toEqual(['deletedREADME.md', 'changedapp.py', 'newtests/test_app.py'])
    expect(button()!.disabled).toBe(false)
  })

  it('puts the files back after the owner agreed, and shows that it did', async () => {
    const undone = message([{ type: 'text', text: 'Fixed.' }, changes({ undone: true })])
    const undo = vi.spyOn(api, 'undoRun').mockResolvedValue({ message: { ...undone, siblings: [] }, restored: ['README.md', 'app.py', 'tests/test_app.py'], left: [] })
    await draw()
    await click()
    expect(undo).not.toHaveBeenCalled()
    expect(useDialog.getState().current?.message).toBe(undoQuestion(changes()))
    await act(async () => { answer(true) })
    expect(undo).toHaveBeenCalledWith('r1')
    expect(shown().undone).toBe(true)
    expect(useStore.getState().messages[0].siblings).toEqual(['m1'])             // the rest of the message is kept as it was
    await draw()
    expect(host.textContent).toContain('They were put back as they were before it.')
    expect(button()).toBeUndefined()
  })

  it('changes nothing when the owner says no', async () => {
    const undo = vi.spyOn(api, 'undoRun')
    await draw()
    await click()
    await act(async () => { answer(false) })
    expect(undo).not.toHaveBeenCalled()
    expect(shown().undone).toBe(false)
  })

  it('shows what the server said when it could not put the files back', async () => {
    vi.spyOn(api, 'undoRun').mockRejectedValue(new api.ApiError(409, 'A run of this chat is still going: wait for it to end, or stop it, then undo.'))
    await draw()
    await click()
    await act(async () => { answer(true) })
    expect(useStore.getState().error).toBe('A run of this chat is still going: wait for it to end, or stop it, then undo.')
    expect(shown().undone).toBe(false)
    expect(button()!.disabled).toBe(false)
  })

  it('marks the files that were left as they are, and keeps the button for another go', async () => {
    useStore.setState({ messages: [message([changes({ left: ['app.py'] })])] })
    await draw()
    expect(rows()).toEqual(['deletedREADME.md', 'changedapp.py left as it is', 'newtests/test_app.py'])
    expect(button()!.disabled).toBe(false)
  })

  it('offers no undo while a run is going', async () => {
    useStore.setState({ live: { runId: 'r2', target: null, steps: [] } as never })
    await draw()
    expect(button()!.disabled).toBe(true)
    expect(button()!.title).toBe('A run is going: wait for it to end, or stop it.')
  })

  it('shows the reason and no button when no save point was kept', async () => {
    useStore.setState({ messages: [message([changes({ files: [], before: '', after: '', reason: NO_SAVE_POINT })])] })
    await draw()
    expect(host.textContent).toBe(NO_SAVE_POINT)
    expect(button()).toBeUndefined()
  })

  it('shows a name with a character that hides or reorders text as what it is', async () => {
    useStore.setState({ messages: [message([changes({ files: [{ path: 'safe‮txt.exe', change: 'new' }] })])] })
    await draw()
    expect(rows()).toEqual(['newsafe\\u{202E}txt.exe'])
  })

  it('stands under the answer of its run', async () => {
    await act(async () => { root.render(createElement(MessageItem, { message: useStore.getState().messages[0] })) })
    const article = host.querySelector('article')!
    expect(article.querySelector('.run-changes')!.textContent).toContain('This run changed 3 files in /home/dave/project.')
    expect(article.textContent!.indexOf('Fixed.')).toBeLessThan(article.textContent!.indexOf('This run changed'))
    await act(async () => { root.render(createElement(MessageItem, { message: message([{ type: 'text', text: 'Nothing to do.' }]) })) })
    expect(host.querySelector('.run-changes')).toBeNull()
  })

  it('folds a long list away and still counts it', async () => {
    const many = Array.from({ length: 300 }, (_, n) => ({ path: `gen/${n}.txt`, change: 'new' as const }))
    useStore.setState({ messages: [message([changes({ files: many })])] })
    await draw()
    expect(host.querySelector('details')!.open).toBe(false)
    expect(host.querySelector('summary')!.textContent).toBe('The 300 files')
    expect(rows()).toHaveLength(201)
    expect(rows()[200]).toBe('and 100 more')
  })
})
