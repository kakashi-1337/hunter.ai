import { useMemo } from 'react'
import { create } from 'zustand'
import * as api from './api'
import { streamRun } from './sse'
import { applyEvent, pendingAsks, runningCalls, stepsFromEvents } from './tree'
import type { QueuedMessage, Persona, Skill,
  Chat, Message, Mode, Options, PermissionDecision, PermissionRequest, ProviderInfo, RunDetail, RunEvent, RunSummary, SearchHit, Settings, StartedRun, Step, Target,
} from './types'

interface LiveRun {
  runId: string
  chatId: string
  target: string | null
  steps: Step[]
}

/** What the header shows, and a new chat gets, while no chat is open. */
export interface Draft {
  target_id: string | null
  mode: Mode
  provider: string | null   // null: the default for new chats
  model: string | null
  options: Options
  persona_id?: string | null
}
const EMPTY_DRAFT: Draft = { target_id: null, mode: 'pentesting', provider: null, model: null, options: {} }

interface State {
  draft: Draft
  targets: Target[]
  chats: Chat[]
  activeChatId: string | null
  chat: Chat | null
  messages: Message[]
  live: LiveRun | null
  /** Permission requests of the live run that wait for an answer, oldest first. */
  asks: PermissionRequest[]
  /** Tool calls of the live run that are executing right now. */
  runningCalls: string[]
  /** The chat's resolved working folder. */
  workdir: string | null
  runs: RunSummary[]
  selectedRunId: string | null
  runDetail: RunDetail | null
  providers: ProviderInfo[]
  settings: Settings | null
  searchResults: SearchHit[] | null
  error: string | null
  /** What the main area shows. Opening or creating a chat always returns to the chat. */
  view: 'chat' | 'knowledge'
  personas: Persona[]
  refreshPersonas(): Promise<void>
  skills: Skill[]
  refreshSkills(): Promise<void>
  skillsOpen: boolean
  setSkillsOpen(open: boolean): void
  /** The mode a chat was in before a skill's slash command moved it (to plan, say): where /go takes it back. */
  modeBefore: Record<string, Mode>
  rememberMode(chatId: string, mode: Mode): void
  choosePersona(id: string | null): Promise<void>
  setView(view: 'chat' | 'knowledge'): void
  loaded: boolean

  load(): Promise<void>
  refreshLists(): Promise<void>
  refreshProviders(): Promise<void>
  openChat(id: string | null): Promise<void>
  newChat(targetId?: string | null): Promise<void>
  /** Resolves true once the server accepted the prompt; false (with `error` set) when it did not. */
  send(text: string, uploadIds: string[]): Promise<boolean>
  /** Messages sent while the agent was busy, still waiting to be read. */
  queued: QueuedMessage[]
  /** Keep a message for the agent's next step; nothing is interrupted. */
  queueMessage(text: string): Promise<boolean>
  unqueue(id: string): Promise<void>
  /** Send what is still waiting as an ordinary message (the run ended before it was read). */
  sendQueued(): Promise<void>
  edit(messageId: string, text: string): Promise<void>
  retry(messageId: string): Promise<void>
  remove(messageId: string): Promise<void>
  switchBranch(messageId: string): Promise<void>
  cancel(): Promise<void>
  /** Put the files a run changed back as they were before it; the list under its answer then says so. */
  undoRun(runId: string): Promise<void>
  answer(requestId: string, decision: PermissionDecision, note: string): Promise<void>
  patchChat(patch: Parameters<typeof api.updateChat>[1]): Promise<void>
  removeChat(id: string): Promise<void>
  selectRun(id: string | null): Promise<void>
  runSearch(q: string): Promise<void>
  setError(message: string | null): void
}

/** The body that creates a chat from the draft: unset fields are left to the server's defaults. */
const draftBody = (d: Draft) => ({
  target_id: d.target_id, mode: d.mode, options: d.options, ...(d.persona_id ? { persona_id: d.persona_id } : {}),
  ...(d.provider && d.model ? { provider: d.provider, model: d.model } : {}),
})

let stopStream: (() => void) | null = null
/** Stream losses in a row without an event in between. */
let losses = 0
const MAX_LOSSES = 3
const LOST = 'Lost the live connection. Reloaded the saved state of this chat.'
const describe = (e: unknown) => (e instanceof Error ? e.message : String(e))

export const useStore = create<State>((set, get) => {
  /** Run an action; show its failure in the banner instead of throwing into React. */
  const guard = async (f: () => Promise<void>) => {
    try {
      await f()
    } catch (e) {
      set({ error: describe(e) })
    }
  }

  /** The messages waiting in a chat; an empty list when they cannot be had. */
  const waiting = (id: string): Promise<QueuedMessage[]> => Promise.resolve(api.listQueue(id)).then((q) => q ?? []).catch(() => [])

  const reloadChat = async (id: string) => {
    const { chat, messages, workdir } = await api.getChat(id)
    if (get().activeChatId !== id) return
    const runs = await api.listRuns(id)
    if (get().activeChatId !== id) return
    set({ chat, messages, runs, workdir: workdir ?? null })
    waiting(id).then((queued) => { if (get().activeChatId === id) set({ queued }) }).catch(() => {})
  }

  const onEvent = (chatId: string, userMessageId: string | null) => (e: RunEvent) => {
    const s = get()
    if (s.activeChatId !== chatId || !s.live) return
    losses = 0
    const { messages, target } = applyEvent(s.messages, e, s.live.target, userMessageId)
    const live = { ...s.live, target, steps: stepsFromEvents(s.live.steps, e) }
    if (e.type === 'run_finished') {
      stopStream = null
      set({ messages, live: null, asks: [], runningCalls: [], error: e.status === 'error' ? e.error : s.error })
      void guard(async () => {
        await reloadChat(chatId)
        await get().refreshLists()
        await get().selectRun(live.runId)
        // What the run did not get to read goes out as the next message when it ended well; after a stop or an
        // error it stays in view, for the owner to send or drop.
        if (get().activeChatId !== chatId) return
        if (e.status === 'done') await get().sendQueued()
        else set({ queued: await waiting(chatId) })
      })
    } else {
      set({ messages, live, asks: pendingAsks(s.asks, e), runningCalls: runningCalls(s.runningCalls, e) })
      if (e.type === 'step_finished' && e.step.kind === 'user_note') void waiting(chatId).then((queued) => { if (get().activeChatId === chatId) set({ queued }) }).catch(() => {})
    }
  }

  /** The stream dropped. The run may still be going: reload, and follow it again when it is. */
  const onLost = (chatId: string) => () => {
    stopStream = null
    losses += 1
    // A prompt must not stay on screen for a request the server may no longer hold.
    set({ asks: [], runningCalls: [] })
    void (async () => {
      try {
        await reloadChat(chatId)
      } catch {
        if (get().activeChatId === chatId) set({ live: null, error: LOST })
        return
      }
      if (get().activeChatId !== chatId) return
      const running = get().runs.find((r) => r.status === 'running')
      if (running && losses <= MAX_LOSSES) {
        // The replay of the stream restores the pending permission requests.
        follow(chatId, { run_id: running.id, user_message_id: running.user_message_id ?? '', assistant_message_id: running.assistant_message_id ?? '' })
      } else {
        set({ live: null, error: running ? LOST : get().error })
        const runs = get().runs
        if (!running && runs.length) await get().selectRun(runs[runs.length - 1].id)
      }
    })()
  }

  const follow = (chatId: string, started: StartedRun) => {
    stopStream?.()
    set({
      live: { runId: started.run_id, chatId, target: null, steps: [] }, asks: [], runningCalls: [],
      selectedRunId: started.run_id, runDetail: null,
    })
    stopStream = streamRun(started.run_id, onEvent(chatId, started.user_message_id), onLost(chatId))
  }

  /** Resolves true once the server accepted the request, whatever happens to the view afterwards. */
  const start = async (f: (chatId: string) => Promise<StartedRun>) => {
    let accepted = false
    await guard(async () => {
      const chatId = get().activeChatId
      if (!chatId) return
      const started = await f(chatId)
      accepted = true
      // The user may have moved to another chat meanwhile: the run goes on server-side, this view is not its.
      if (get().activeChatId !== chatId) return
      await reloadChat(chatId)
      if (get().activeChatId !== chatId) return
      follow(chatId, started)
    })
    return accepted
  }

  return {
    view: 'chat', setView: (view) => set({ view }), personas: [],
    draft: EMPTY_DRAFT, targets: [], chats: [], activeChatId: null, chat: null, messages: [], live: null, asks: [], runningCalls: [],
    workdir: null, runs: [],
    selectedRunId: null, runDetail: null, providers: [], settings: null, searchResults: null, error: null,
    loaded: false,

    setError: (error) => set({ error }),

    load: () =>
      guard(async () => {
        await get().refreshLists()
        set({ settings: await api.getSettings(), loaded: true })
        await get().refreshPersonas()
        await get().refreshSkills()
        await get().refreshProviders()
      }),

    refreshLists: async () => {
      const [targets, chats] = await Promise.all([api.listTargets(), api.listChats()])
      set({ targets, chats })
    },

    refreshProviders: async () => set({ providers: await api.listProviders() }),
    refreshPersonas: async () => { try { set({ personas: await api.listPersonas() }) } catch { /* the picker stays as it was */ } },
    skills: [], skillsOpen: false, modeBefore: {},
    refreshSkills: async () => { try { set({ skills: await api.listSkills() }) } catch { /* the slash list stays as it was */ } },
    setSkillsOpen: (skillsOpen) => set({ skillsOpen }),
    rememberMode: (chatId, mode) => set({ modeBefore: { ...get().modeBefore, [chatId]: mode } }),

    choosePersona: async (id) => {
      const persona = get().personas.find((p) => p.id === id) ?? null
      // A fresh chat also takes the persona's mode and model; one with messages keeps its own.
      const fresh = !get().activeChatId || get().messages.length === 0
      await get().patchChat({
        persona_id: persona?.id ?? null,
        ...(fresh && persona?.default_mode ? { mode: persona.default_mode } : {}),
        ...(fresh && persona?.provider && persona.model ? { provider: persona.provider, model: persona.model } : {}),
      })
    },

    openChat: (id) =>
      guard(async () => {
        stopStream?.()
        stopStream = null
        losses = 0
        set({
          view: 'chat', activeChatId: id, chat: null, messages: [], live: null, asks: [], runningCalls: [], workdir: null, runs: [], queued: [],
          selectedRunId: null, runDetail: null,
        })
        if (!id) return
        await reloadChat(id)
        if (get().activeChatId !== id) return
        const runs = get().runs
        const running = runs.find((r) => r.status === 'running')
        if (running) {
          // A run is still going (page reload, or opened from another tab): rejoin it.
          follow(id, { run_id: running.id, user_message_id: running.user_message_id ?? '', assistant_message_id: running.assistant_message_id ?? '' })
        } else if (runs.length) {
          await get().selectRun(runs[runs.length - 1].id)
        }
      }),

    newChat: (targetId) =>
      guard(async () => {
        const chat = await api.createChat({ ...draftBody(get().draft), target_id: targetId !== undefined ? targetId : get().draft.target_id })
        await get().refreshLists()
        await get().openChat(chat.id)
      }),

    queued: [],
    queueMessage: async (text) => {
      const chatId = get().activeChatId
      if (!chatId || !text.trim()) return false
      try {
        const q = await api.addQueue(chatId, text)
        if (get().activeChatId === chatId) set({ queued: [...get().queued, q] })
        return true
      } catch (e) {
        // The run ended in the meantime: it is an ordinary message now.
        if (e instanceof api.ApiError && e.status === 409) return get().send(text, [])
        set({ error: e instanceof Error ? e.message : String(e) })
        return false
      }
    },
    unqueue: (id) => guard(async () => {
      const chatId = get().activeChatId
      if (!chatId) return
      await api.removeQueue(chatId, id).catch(() => {})
      set({ queued: get().queued.filter((q) => q.id !== id) })
    }),
    sendQueued: async () => {
      const chatId = get().activeChatId
      if (!chatId || get().live) return
      const left = (await api.takeQueue(chatId)) ?? []
      set({ queued: [] })
      if (left.length) await get().send(left.map((q) => q.text).join('\n\n'), [])
    },
    send: async (text, uploadIds) => {
      if (!get().activeChatId) {
        await guard(async () => {
          const chat = await api.createChat(draftBody(get().draft))
          await get().refreshLists()
          await get().openChat(chat.id)
        })
      }
      return start((chatId) => api.sendMessage(chatId, text, uploadIds))
    },
    edit: async (messageId, text) => { await start(() => api.editMessage(messageId, text)) },
    retry: async (messageId) => { await start(() => api.retryMessage(messageId)) },

    remove: (messageId) =>
      guard(async () => {
        const id = get().activeChatId
        await api.deleteMessage(messageId)
        if (id) await reloadChat(id)
      }),

    switchBranch: (messageId) =>
      guard(async () => {
        const id = get().activeChatId
        if (!id || get().live) return
        await api.setActiveLeaf(id, messageId)
        await reloadChat(id)
      }),

    cancel: () =>
      guard(async () => {
        const live = get().live
        if (live) await api.cancelRun(live.runId)
      }),

    undoRun: (runId) =>
      guard(async () => {
        const done = await api.undoRun(runId)
        // Only the blocks changed (the list now says what was put back); the rest of the message stays as the page has it.
        set({ messages: get().messages.map((m) => (m.id === done.message.id ? { ...m, blocks: done.message.blocks } : m)) })
      }),

    answer: (requestId, decision, note) =>
      guard(async () => {
        const live = get().live
        if (live) await api.answerPermission(live.runId, requestId, decision, note)
      }),

    patchChat: (patch) =>
      guard(async () => {
        const id = get().activeChatId
        if (!id) {
          // No chat yet: the choice is kept and used when the first message creates the chat.
          const d = { ...get().draft }
          if ('target_id' in patch) d.target_id = patch.target_id ?? null
          if (patch.mode) d.mode = patch.mode
          if (patch.provider) d.provider = patch.provider
          if (patch.model) d.model = patch.model
          if (patch.options) d.options = patch.options
          if ('persona_id' in patch) d.persona_id = patch.persona_id ?? null
          set({ draft: d })
          return
        }
        const chat = await api.updateChat(id, patch)
        set({ chat })
        await get().refreshLists()
      }),

    removeChat: (id) =>
      guard(async () => {
        await api.deleteChat(id)
        if (get().activeChatId === id) await get().openChat(null)
        await get().refreshLists()
      }),

    selectRun: async (id) => {
      set({ selectedRunId: id, runDetail: null })
      if (!id) return
      try {
        const detail = await api.getRun(id)
        if (get().selectedRunId === id) set({ runDetail: detail })
      } catch (e) {
        set({ error: describe(e) })
      }
    },

    runSearch: (text) =>
      guard(async () => {
        if (!text.trim()) return set({ searchResults: null })
        const results = await api.search(text)
        set({ searchResults: results })
      }),
  }
})

/**
 * The chat the header works on: the open chat, or a stand-in built from the draft when none is
 * open (its id is empty), so every control is there before the first message.
 */
export function useChatView(): Chat | null {
  const chat = useStore((s) => s.chat)
  const active = useStore((s) => s.activeChatId)
  const draft = useStore((s) => s.draft)
  const settings = useStore((s) => s.settings)
  return useMemo(() => {
    if (chat) return chat
    if (active || !settings) return null      // a chat is loading, or the app is still starting
    return {
      id: '', title: 'New chat', target_id: draft.target_id, mode: draft.mode,
      provider: draft.provider ?? settings.default_provider, model: draft.model ?? settings.default_model,
      options: draft.options, active_leaf_id: null, created_at: '', updated_at: '', workdir: null, permissions: {}, persona_id: draft.persona_id ?? null,
    }
  }, [chat, active, draft, settings])
}
