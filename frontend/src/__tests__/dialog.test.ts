import { beforeEach, describe, expect, it } from 'vitest'
import { answer, confirmBox, promptBox, useDialog } from '../dialog'

beforeEach(() => useDialog.setState({ current: null, waiting: [] }))

describe('own dialogs', () => {
  it('a question waits on screen until it is answered', async () => {
    const asked = confirmBox('Delete "notes" and all its messages?')
    const c = useDialog.getState().current!
    expect([c.kind, c.message, c.danger]).toEqual(['confirm', 'Delete "notes" and all its messages?', true])
    answer(true)
    expect(await asked).toBe(true)
    expect(useDialog.getState().current).toBeNull()
    const refused = confirmBox('Ingest it again now?')
    expect(useDialog.getState().current!.danger).toBe(false)
    answer(false)
    expect(await refused).toBe(false)
  })
  it('a question the caller marks as dangerous gets the red button whatever its first word', async () => {
    const asked = confirmBox('Switch auto-approve on for this chat?', true)
    expect(useDialog.getState().current!.danger).toBe(true)
    answer(false)
    expect(await asked).toBe(false)
  })
  it('a second question waits for the first', async () => {
    const a = confirmBox('Remove the server "x"?')
    const b = promptBox('New name', 'old')
    expect(useDialog.getState().current!.message).toBe('Remove the server "x"?')
    answer(false)
    expect(await a).toBe(false)
    expect([useDialog.getState().current!.kind, useDialog.getState().current!.initial]).toEqual(['prompt', 'old'])
    answer('new name')
    expect(await b).toBe('new name')
  })
  it('a cancelled prompt gives null, and an answer with nothing on screen does nothing', async () => {
    const p = promptBox('New name')
    answer(null)
    expect(await p).toBeNull()
    answer(true)
    expect(useDialog.getState().current).toBeNull()
  })
})
