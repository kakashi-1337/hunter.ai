import { describe, expect, it } from 'vitest'
import { slashCommand, slashMenu, type SlashContext } from '../slash'

const ctx = (over: Partial<SlashContext> = {}): SlashContext => ({
  busy: false, provider: 'anthropic', model: 'claude-fable-5-2',
  targets: [{ id: 't1', name: 'Acme' }, { id: 't2', name: 'Acme Labs' }, { id: 't3', name: 'Globex' }],
  personas: [{ id: 'p1', name: 'Web, API, and cloud', description: 'Web applications' }, { id: 'p2', name: 'Coding', description: 'Writing code' }],
  providers: [
    { name: 'anthropic', configured: true, models: [
      { id: 'claude-fable-5-2', label: 'Claude Fable 5.2', supports_images: true, options: ['effort', 'max_tokens'], context_window: 1_000_000 },
      { id: 'claude-haiku-5-5', label: 'Claude Haiku 5.5', supports_images: true, options: ['max_tokens'], context_window: 200_000 },
    ] },
    { name: 'ollama', configured: true, models: [{ id: 'qwen3:14b', label: 'qwen3:14b', supports_images: false, options: ['temperature'], context_window: null }] },
    { name: 'openai', configured: false, models: [{ id: 'gpt-x', label: 'gpt-x', supports_images: true, options: ['effort'], context_window: null }] },
  ],
  ...over,
})

describe('what counts as a command', () => {
  it('leaves ordinary messages alone, also when they start with a path', () => {
    for (const text of ['hello', ' /mode plan', '/etc/passwd is readable', '/api/v1/users returns 500', '/', '/unknown thing', '/new endpoint is broken', '/stop the server and look', 'a\n/mode plan'])
      expect(slashCommand(text, ctx())).toBeNull()
  })
  it('reads the commands that take no argument only when they stand alone', () => {
    expect(slashCommand('/new', ctx())).toEqual({ kind: 'new' })
    expect(slashCommand('/NEW  ', ctx())).toEqual({ kind: 'new' })
    expect(slashCommand('/stop', ctx({ busy: true }))).toEqual({ kind: 'stop' })
    expect(slashCommand('/stop', ctx())).toEqual({ kind: 'refused', message: 'Nothing is running.' })
  })
})

describe('commands with a choice', () => {
  it('sets the mode', () => {
    expect(slashCommand('/mode plan', ctx())).toEqual({ kind: 'mode', mode: 'plan' })
    expect(slashCommand('/mode  PENT ', ctx())).toEqual({ kind: 'mode', mode: 'pentesting' })
    expect(slashCommand('/mode', ctx())).toEqual({ kind: 'refused', message: '/mode needs one of: plan, pentesting, coding.' })
    expect(slashCommand('/mode fast', ctx())).toEqual({ kind: 'refused', message: 'No mode matches "fast". There is: plan, pentesting, coding.' })
  })
  it('picks a target by its name, the start of it, or a part of it', () => {
    expect(slashCommand('/target globex', ctx())).toEqual({ kind: 'target', id: 't3' })
    expect(slashCommand('/target glo', ctx())).toEqual({ kind: 'target', id: 't3' })
    expect(slashCommand('/target acme', ctx())).toEqual({ kind: 'target', id: 't1' })            // the whole name wins over a longer one
    expect(slashCommand('/target labs', ctx())).toEqual({ kind: 'target', id: 't2' })
    expect(slashCommand('/target ac', ctx())).toEqual({ kind: 'refused', message: '"ac" fits more than one target: Acme, Acme Labs.' })
    expect(slashCommand('/target none', ctx())).toEqual({ kind: 'target', id: null })
    expect(slashCommand('/target', ctx({ targets: [] }))).toEqual({ kind: 'refused', message: '/target needs one of: none.' })
  })
  it('picks a persona and a model the same way', () => {
    expect(slashCommand('/persona coding', ctx())).toEqual({ kind: 'persona', id: 'p2' })
    expect(slashCommand('/persona none', ctx())).toEqual({ kind: 'persona', id: null })
    expect(slashCommand('/model haiku', ctx())).toEqual({ kind: 'model', provider: 'anthropic', model: 'claude-haiku-5-5' })
    expect(slashCommand('/model qwen3:14b', ctx())).toEqual({ kind: 'model', provider: 'ollama', model: 'qwen3:14b' })
    expect(slashCommand('/model ollama/qwen3:14b', ctx())).toEqual({ kind: 'model', provider: 'ollama', model: 'qwen3:14b' })
    expect(slashCommand('/model gpt-x', ctx())).toEqual({ kind: 'refused', message: 'No model matches "gpt-x". There is: anthropic/claude-fable-5-2, anthropic/claude-haiku-5-5, ollama/qwen3:14b.' })
  })
  it('sets the effort only for a model that has one', () => {
    expect(slashCommand('/effort high', ctx())).toEqual({ kind: 'effort', effort: 'high' })
    expect(slashCommand('/effort default', ctx())).toEqual({ kind: 'effort', effort: undefined })
    expect(slashCommand('/effort high', ctx({ model: 'claude-haiku-5-5' }))).toEqual({ kind: 'refused', message: 'anthropic/claude-haiku-5-5 has no effort setting.' })
  })
  it('waits for a run to end before it changes the chat', () => {
    expect(slashCommand('/mode plan', ctx({ busy: true }))).toEqual({ kind: 'refused', message: 'The agent is working: /stop it or wait, then change the mode.' })
    expect(slashCommand('/new', ctx({ busy: true }))).toEqual({ kind: 'new' })
  })
})

describe('the menu while typing', () => {
  const labels = (text: string, c = ctx()) => slashMenu(text, c)?.map((i) => i.label) ?? null
  it('lists every command for a lone slash and narrows as letters come', () => {
    expect(labels('/')).toEqual(['/new', '/mode', '/model', '/persona', '/target', '/effort', '/stop'])
    expect(labels('/mo')).toEqual(['/mode', '/model'])
    expect(labels('/MOD')).toEqual(['/mode', '/model'])
    expect(labels('/x')).toBeNull()
  })
  it('stays away from text that is not a command being typed', () => {
    for (const text of ['', 'hello /', ' /', '/etc/', '/mode plan\nmore', '/new chat'])
      expect(slashMenu(text, ctx())).toBeNull()
  })
  it('completes a command name to the point where its argument starts', () => {
    expect(slashMenu('/mo', ctx())).toEqual([
      { label: '/mode', hint: 'plan, pentesting or coding', text: '/mode ', run: false },
      { label: '/model', hint: 'the model this chat talks to', text: '/model ', run: false },
    ])
    expect(slashMenu('/ne', ctx())).toEqual([{ label: '/new', hint: 'a new chat on the same target', text: '/new', run: true }])
  })
  it('offers the values of an argument', () => {
    expect(slashMenu('/mode ', ctx())).toEqual([
      { label: 'plan', hint: '', text: '/mode plan', run: true },
      { label: 'pentesting', hint: '', text: '/mode pentesting', run: true },
      { label: 'coding', hint: '', text: '/mode coding', run: true },
    ])
    expect(labels('/mode pe')).toEqual(['pentesting'])
    expect(labels('/target ')).toEqual(['none', 'Acme', 'Acme Labs', 'Globex'])
    expect(labels('/target lab')).toEqual(['Acme Labs'])
    expect(labels('/target acme')).toEqual(['Acme', 'Acme Labs'])          // still typing: the longer name stays on offer
    expect(labels('/model 5')).toEqual(['anthropic/claude-fable-5-2', 'anthropic/claude-haiku-5-5'])
    expect(labels('/persona ')).toEqual(['none', 'Web, API, and cloud', 'Coding'])
    expect(labels('/model ')).toEqual(['anthropic/claude-fable-5-2', 'anthropic/claude-haiku-5-5', 'ollama/qwen3:14b'])
    expect(slashMenu('/model hai', ctx())).toEqual([{ label: 'anthropic/claude-haiku-5-5', hint: 'Claude Haiku 5.5', text: '/model anthropic/claude-haiku-5-5', run: true }])
    expect(labels('/effort ')).toEqual(['default', 'low', 'medium', 'high', 'xhigh', 'max'])
    expect(labels('/mode zzz')).toEqual([])
  })
})
