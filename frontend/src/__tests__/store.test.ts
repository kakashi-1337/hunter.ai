import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api', () => ({
  listTargets: vi.fn(),
  listChats: vi.fn(),
  createChat: vi.fn(),
  updateChat: vi.fn(),
  getChat: vi.fn(),
  listRuns: vi.fn(),
  sendMessage: vi.fn(),
  deleteMessage: vi.fn(),
  getRun: vi.fn(),
  cancelRun: vi.fn(),
  answerPermission: vi.fn(),
  listQueue: vi.fn(),
  takeQueue: vi.fn(),
  addQueue: vi.fn(),
  removeQueue: vi.fn(),
}))
vi.mock('../sse', () => ({ streamRun: vi.fn() }))

import * as api from '../api'
import { streamRun } from '../sse'
import { useStore } from '../store'

const m = vi.mocked(api)
const stream = vi.mocked(streamRun)

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const chat = (id: string) => ({ id, title: `chat ${id}` }) as never
const msg = (chatId: string) => ({ id: `m-${chatId}`, chat_id: chatId, role: 'user', text: `hello from ${chatId}` }) as never
const run = (id: string, status: string) => ({ id, status, user_message_id: `u-${id}`, assistant_message_id: `a-${id}` }) as never
const started = (runId: string) => ({ run_id: runId, user_message_id: `u-${runId}`, assistant_message_id: `a-${runId}` })

/** Runs per chat id; tests overwrite entries to shape a scenario. */
let runsByChat: Record<string, unknown[]>

beforeEach(async () => {
  vi.clearAllMocks()
  runsByChat = {}
  m.listTargets.mockResolvedValue([])
  m.listChats.mockResolvedValue([])
  m.getChat.mockImplementation(async (id) => ({ chat: chat(id), messages: [msg(id)] }))
  m.listRuns.mockImplementation(async (id) => (runsByChat[id] ?? []) as never)
  m.getRun.mockImplementation(async (id) => ({ id }) as never)
  m.deleteMessage.mockResolvedValue(undefined)
  stream.mockReturnValue(() => {})
  await useStore.getState().openChat(null)
  useStore.setState({ error: null })
  vi.clearAllMocks()
  stream.mockReturnValue(() => {})
})

describe('switching chats while a request is in flight', () => {
  it('start(): a run started in chat A is not attached to chat B', async () => {
    const s = useStore.getState()
    await s.openChat('A')
    const post = deferred<ReturnType<typeof started>>()
    m.sendMessage.mockReturnValue(post.promise)

    const sending = s.send('hi', [])
    await vi.waitFor(() => expect(m.sendMessage).toHaveBeenCalledWith('A', 'hi', []))
    await s.openChat('B')
    post.resolve(started('run-A'))
    await sending

    const after = useStore.getState()
    expect(after.error).toBeNull()
    expect(after.activeChatId).toBe('B')
    expect(after.live).toBeNull()
    expect(stream).not.toHaveBeenCalled()
    expect(after.chat).toEqual(chat('B'))
  })

  it('start(): a switch during the reload after the POST does not attach the run either', async () => {
    const s = useStore.getState()
    await s.openChat('A')
    m.sendMessage.mockResolvedValue(started('run-A'))
    const chatA = deferred<never>()
    m.getChat.mockImplementation((id) =>
      id === 'A' ? chatA.promise : Promise.resolve({ chat: chat(id), messages: [msg(id)] }))
    m.getChat.mockClear()

    const sending = s.send('hi', [])
    await vi.waitFor(() => expect(m.getChat).toHaveBeenCalledWith('A'))
    await s.openChat('B')
    chatA.resolve({ chat: chat('A'), messages: [msg('A')] } as never)
    await sending

    const after = useStore.getState()
    expect(after.error).toBeNull()
    expect(after.live).toBeNull()
    expect(stream).not.toHaveBeenCalled()
    expect(after.chat).toEqual(chat('B'))
  })

  it('reloadChat(): a late runs list for chat A is not painted under chat B', async () => {
    const s = useStore.getState()
    await s.openChat('A')
    const runsA = deferred<never>()
    m.listRuns.mockImplementation((id) => (id === 'A' ? runsA.promise : Promise.resolve([] as never)))
    m.listRuns.mockClear()

    const removing = s.remove('m-A')
    await vi.waitFor(() => expect(m.listRuns).toHaveBeenCalledWith('A'))
    await s.openChat('B')
    runsA.resolve([run('run-A', 'done')] as never)
    await removing

    const after = useStore.getState()
    expect(after.error).toBeNull()
    expect(after.chat).toEqual(chat('B'))
    expect(after.messages).toEqual([msg('B')])
    expect(after.runs).toEqual([])
  })

  it('openChat(): a stale open of chat A does not re-follow the run of chat B as its own', async () => {
    runsByChat.B = [run('run-B', 'running')]
    const s = useStore.getState()
    const chatA = deferred<never>()
    m.getChat.mockImplementation((id) =>
      id === 'A' ? chatA.promise : Promise.resolve({ chat: chat(id), messages: [msg(id)] }))

    const openingA = s.openChat('A')
    await vi.waitFor(() => expect(m.getChat).toHaveBeenCalledWith('A'))
    await s.openChat('B')
    expect(useStore.getState().live?.chatId).toBe('B')
    chatA.resolve({ chat: chat('A'), messages: [msg('A')] } as never)
    await openingA

    const after = useStore.getState()
    expect(after.error).toBeNull()
    expect(after.live).toEqual({ runId: 'run-B', chatId: 'B', target: null, steps: [] })
    expect(stream).toHaveBeenCalledTimes(1)
  })
})

describe('send() reports whether the prompt was accepted', () => {
  it('resolves true once the run has started', async () => {
    const s = useStore.getState()
    await s.openChat('A')
    m.sendMessage.mockResolvedValue(started('run-A'))

    await expect(s.send('hi', [])).resolves.toBe(true)
    expect(useStore.getState().live?.runId).toBe('run-A')
    expect(useStore.getState().error).toBeNull()
  })

  it('resolves false and shows the error when the POST is refused', async () => {
    const s = useStore.getState()
    await s.openChat('A')
    m.sendMessage.mockRejectedValue(new Error('A run is already going in this chat.'))

    await expect(s.send('hi', ['up-1'])).resolves.toBe(false)
    expect(useStore.getState().error).toBe('A run is already going in this chat.')
    expect(useStore.getState().live).toBeNull()
  })

  it('resolves false and shows the error when the chat cannot be created', async () => {
    m.createChat.mockRejectedValue(new Error('Cannot reach the HunterAI backend. Is it running?'))

    await expect(useStore.getState().send('hi', [])).resolves.toBe(false)
    expect(useStore.getState().error).toBe('Cannot reach the HunterAI backend. Is it running?')
    expect(m.sendMessage).not.toHaveBeenCalled()
  })

  it('resolves true when the POST succeeded but the user moved to another chat', async () => {
    const s = useStore.getState()
    await s.openChat('A')
    const post = deferred<ReturnType<typeof started>>()
    m.sendMessage.mockReturnValue(post.promise)

    const sending = s.send('hi', [])
    await vi.waitFor(() => expect(m.sendMessage).toHaveBeenCalled())
    await s.openChat('B')
    post.resolve(started('run-A'))

    await expect(sending).resolves.toBe(true)
  })
})

describe('losing the live stream', () => {
  it('rejoins a running run when the stream is lost, and gives up when it is over', async () => {
    runsByChat['A'] = [run('r1', 'running')]
    await useStore.getState().openChat('A')
    const onLost = stream.mock.calls[0][2]
    onLost()
    await vi.waitFor(() => expect(stream).toHaveBeenCalledTimes(2))
    expect(useStore.getState().live?.runId).toBe('r1')
    runsByChat['A'] = [run('r1', 'interrupted')]
    stream.mock.calls[1][2]()
    await vi.waitFor(() => expect(useStore.getState().live).toBeNull())
  })
  it('clears the prompt at once when the stream is lost, and gives up when the reload fails', async () => {
    runsByChat['A'] = [run('r1', 'running')]
    await useStore.getState().openChat('A')
    const [, onEvent, onLost] = stream.mock.calls[0]
    onEvent({ type: 'permission_request', request: { id: 'p1', tool_call_id: 'c1' } } as never)
    expect(useStore.getState().asks.map((a) => a.id)).toEqual(['p1'])
    m.getChat.mockRejectedValueOnce(new Error('down'))
    onLost()
    expect(useStore.getState().asks).toEqual([])
    await vi.waitFor(() => expect(useStore.getState().live).toBeNull())
    expect(useStore.getState().error).toBeTruthy()
    expect(stream).toHaveBeenCalledTimes(1)
  })
  it('answers the pending request of the live run', async () => {
    runsByChat['A'] = [run('r1', 'running')]
    await useStore.getState().openChat('A')
    m.answerPermission.mockResolvedValue({ ok: true })
    await useStore.getState().answer('p1', 'deny', 'no')
    expect(m.answerPermission).toHaveBeenCalledWith('r1', 'p1', 'deny', 'no')
  })
})

describe('choices made before a chat exists', () => {
  it('are kept as a draft and used when the first message creates the chat', async () => {
    const s = useStore.getState()
    useStore.setState({ draft: { target_id: null, mode: 'pentesting', provider: null, model: null, options: {} } })
    await s.patchChat({ mode: 'plan' })
    await s.patchChat({ provider: 'ollama', model: 'qwen3:8b', options: { temperature: 0.2 } })
    await s.patchChat({ target_id: 't1' })
    expect(useStore.getState().draft).toEqual({ target_id: 't1', mode: 'plan', provider: 'ollama', model: 'qwen3:8b', options: { temperature: 0.2 } })
    m.createChat.mockResolvedValue(chat('N'))
    m.sendMessage.mockResolvedValue(started('r9'))
    await useStore.getState().send('hello', [])
    expect(m.createChat).toHaveBeenCalledWith({ target_id: 't1', mode: 'plan', provider: 'ollama', model: 'qwen3:8b', options: { temperature: 0.2 } })
    expect(m.sendMessage).toHaveBeenCalledWith('N', 'hello', [])
  })
  it('leaves the model to the server default when none was picked', async () => {
    useStore.setState({ draft: { target_id: null, mode: 'coding', provider: null, model: null, options: {} } })
    m.createChat.mockResolvedValue(chat('N2'))
    await useStore.getState().newChat()
    expect(m.createChat).toHaveBeenCalledWith({ target_id: null, mode: 'coding', options: {} })
  })
})

describe('new chat with an explicit "no target"', () => {
  it('does not fall back to the draft target', async () => {
    useStore.setState({ draft: { target_id: 't1', mode: 'pentesting', provider: null, model: null, options: {} } })
    m.createChat.mockResolvedValue(chat('N3'))
    await useStore.getState().newChat(null)
    expect(m.createChat).toHaveBeenLastCalledWith({ target_id: null, mode: 'pentesting', options: {} })
  })
})

describe('choosing a persona', () => {
  const persona = { id: 'p1', name: 'RE', description: '', system_prompt: 'x', default_mode: 'coding' as const, provider: 'ollama', model: 'qwen3:8b', tools: [], collections: [], builtin: false, created_at: 't', updated_at: 't' }
  it('before a chat exists it sets the draft, and the first message creates the chat with it', async () => {
    useStore.setState({ personas: [persona], activeChatId: null, chat: null, messages: [], draft: { target_id: null, mode: 'pentesting', provider: null, model: null, options: {} } })
    await useStore.getState().choosePersona('p1')
    expect(useStore.getState().draft).toMatchObject({ persona_id: 'p1', mode: 'coding', provider: 'ollama', model: 'qwen3:8b' })
    m.createChat.mockResolvedValue(chat('NP'))
    await useStore.getState().newChat()
    expect(m.createChat).toHaveBeenLastCalledWith({ target_id: null, mode: 'coding', options: {}, persona_id: 'p1', provider: 'ollama', model: 'qwen3:8b' })
  })
  it('on a chat that already has messages it changes only the persona', async () => {
    useStore.setState({ personas: [persona], activeChatId: 'c1', chat: chat('c1'), messages: [{ id: 'm1' } as never] })
    m.updateChat.mockResolvedValue(chat('c1'))
    await useStore.getState().choosePersona('p1')
    expect(m.updateChat).toHaveBeenLastCalledWith('c1', { persona_id: 'p1' })
    await useStore.getState().choosePersona(null)
    expect(m.updateChat).toHaveBeenLastCalledWith('c1', { persona_id: null })
  })
})
