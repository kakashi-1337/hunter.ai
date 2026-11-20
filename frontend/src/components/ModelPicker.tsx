import { useChatView, useStore } from '../store'
import { providerGroup, pruneOptions } from '../tree'
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
      {providers.map((p) => {
        const { label, empty } = providerGroup(p)
        return (
          <optgroup key={p.name} label={label}>
            {p.configured && empty && <option disabled>{empty}</option>}
            {p.models.map((m) => (
              <option key={m.id} value={`${p.name}\u0000${m.id}`}>{m.label === m.id ? m.id : `${m.label} (${m.id})`}</option>
            ))}
            {!p.configured && <option disabled>{empty}</option>}
          </optgroup>
        )
      })}
    </select>
  )
}
