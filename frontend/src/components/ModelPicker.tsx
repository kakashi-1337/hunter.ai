import { useChatView, useStore } from '../store'
import { pruneOptions } from '../tree'
import type { ModelInfo } from '../types'

export function useModelInfo(): ModelInfo | null {
  const chat = useChatView()
  const providers = useStore((s) => s.providers)
  if (!chat) return null
  return providers.find((p) => p.name === chat.provider)?.models.find((m) => m.id === chat.model) ?? null
}

export function ModelPicker() {
  const chat = useChatView()
  const providers = useStore((s) => s.providers)
  const busy = useStore((s) => s.live !== null)
  const patchChat = useStore((s) => s.patchChat)
  if (!chat) return null
  const { provider, model } = chat
  const current = `${provider}\u0000${model}`
  const listed = providers.some((p) => p.name === provider && p.models.some((m) => m.id === model))

  const choose = (value: string) => {
    const [nextProvider, nextModel] = value.split('\u0000')
    const info = providers.find((p) => p.name === nextProvider)?.models.find((m) => m.id === nextModel)
    // Options the new model does not accept are dropped rather than sent and rejected.
    void patchChat({ provider: nextProvider, model: nextModel, options: pruneOptions(chat.options, info?.options ?? []) })
  }

  return (
    <select aria-label="Model" value={current} disabled={busy} onChange={(e) => choose(e.target.value)} title={`${provider}/${model}`}>
      {!listed && <option value={current}>{provider}/{model}</option>}
      {providers.map((p) => (
        <optgroup key={p.name} label={p.configured ? p.name : p.name === 'codex' ? 'codex — install the codex CLI' : p.name === 'claude_code' ? 'claude_code — install the claude CLI and log in' : `${p.name} — add key in settings`}>
          {p.configured && p.models.length === 0 && <option disabled>no models available</option>}
          {p.models.map((m) => (
            <option key={m.id} value={`${p.name}\u0000${m.id}`}>{m.label === m.id ? m.id : `${m.label} (${m.id})`}</option>
          ))}
          {!p.configured && <option disabled>not configured</option>}
        </optgroup>
      ))}
    </select>
  )
}
