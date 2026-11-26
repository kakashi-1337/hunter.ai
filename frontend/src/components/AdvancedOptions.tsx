import { useEffect, useRef, useState } from 'react'
import { SAMPLING, effortChoices } from '../options'
import { useChatView, useStore } from '../store'
import { optionsNote, pruneOptions } from '../tree'
import type { Options } from '../types'
import { useModelInfo } from './ModelPicker'

const VERBOSITY = ['low', 'medium', 'high'] as const

/** `rest`: where the slider stands while the option is not set: the value that changes nothing (the least one unless given). */
function Slider({ label, value, min, max, step, rest = min, onChange }: { label: string; value: number | undefined; min: number; max: number; step: number; rest?: number; onChange: (v: number | undefined) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <div className="range">
        <input type="range" min={min} max={max} step={step} value={value ?? rest} onChange={(e) => onChange(Number(e.target.value))} />
        <output>{value ?? '—'}</output>
        <button className="icon" title="Use the model's default" disabled={value === undefined} onClick={(e) => { e.preventDefault(); onChange(undefined) }}>reset</button>
      </div>
    </label>
  )
}

/** A whole number of `least` or more (1 unless given); anything else leaves the option unset. */
function NumberField({ label, value, placeholder, least = 1, onChange }: { label: string; value: number | undefined; placeholder: string; least?: number; onChange: (v: number | undefined) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input type="number" min={least} step={1} value={value ?? ''} placeholder={placeholder}
        onChange={(e) => { const n = Math.floor(Number(e.target.value)); onChange(e.target.value === '' || !Number.isFinite(n) || n < least ? undefined : n) }} />
    </label>
  )
}

export function AdvancedOptions() {
  const chat = useChatView()
  const patchChat = useStore((s) => s.patchChat)
  const info = useModelInfo()
  const providers = useStore((s) => s.providers)
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<Options>({})
  const root = useRef<HTMLDivElement>(null)
  const allowed = info?.options ?? []
  const has = (k: string) => allowed.includes(k)
  const sampling = SAMPLING.some(has)

  useEffect(() => { if (open && chat) setDraft(chat.options) }, [open, chat])
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('mousedown', close)
    window.addEventListener('keydown', esc)
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('keydown', esc) }
  }, [open])

  if (!chat) return null
  const set = (patch: Options) => {
    const next = pruneOptions({ ...draft, ...patch }, allowed)
    setDraft(next)
    void patchChat({ options: next })
  }
  const active = Object.keys(pruneOptions(chat.options, allowed)).length
  const note = optionsNote(chat.provider, chat.model, providers)

  return (
    <div className="popover" ref={root}>
      <button className="btn" aria-expanded={open} onClick={() => setOpen((o) => !o)}>Options{active ? ` · ${active}` : ''}</button>
      {open && (
        <div className="sheet" role="dialog" aria-label="Model options">
          {note && <div className="hint">{note}</div>}
          {has('temperature') && <Slider label="Temperature" value={draft.temperature} min={0} max={2} step={0.1} onChange={(v) => set({ temperature: v })} />}
          {has('top_p') && <Slider label="Top P" value={draft.top_p} min={0} max={1} step={0.05} onChange={(v) => set({ top_p: v })} />}
          {has('max_tokens') && <NumberField label="Max output tokens" value={draft.max_tokens} placeholder="model default" onChange={(v) => set({ max_tokens: v })} />}
          {has('context_window') && <NumberField label="Context window (tokens)" value={draft.context_window} placeholder={info?.context_window ? String(info.context_window) : 'model default'} onChange={(v) => set({ context_window: v })} />}
          {has('effort') && (
            <label className="field"><span>Reasoning effort</span>
              <select value={draft.effort ?? ''} onChange={(e) => set({ effort: (e.target.value || undefined) as Options['effort'] })}>
                <option value="">model default</option>
                {effortChoices(info?.efforts, draft.effort).map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
            </label>
          )}
          {has('thinking') && (
            <label className="field"><span>Thinking</span>
              <select value={draft.thinking === undefined ? '' : draft.thinking ? 'on' : 'off'}
                onChange={(e) => set({ thinking: e.target.value === '' ? undefined : e.target.value === 'on' })}>
                <option value="">model default</option><option value="on">on</option><option value="off">off</option>
              </select>
            </label>
          )}
          {has('thinking_budget') && <NumberField label="Thinking budget (tokens)" value={draft.thinking_budget} placeholder="model default" onChange={(v) => set({ thinking_budget: v })} />}
          {has('verbosity') && (
            <label className="field"><span>Verbosity</span>
              <select value={draft.verbosity ?? ''} onChange={(e) => set({ verbosity: (e.target.value || undefined) as Options['verbosity'] })}>
                <option value="">model default</option>
                {VERBOSITY.map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
            </label>
          )}
          {sampling && <div className="section">Sampling</div>}
          {has('top_k') && <NumberField label="Top K" value={draft.top_k} placeholder="model default" onChange={(v) => set({ top_k: v })} />}
          {has('min_p') && <Slider label="Min P" value={draft.min_p} min={0} max={1} step={0.01} onChange={(v) => set({ min_p: v })} />}
          {has('repeat_penalty') && <Slider label="Repeat penalty" value={draft.repeat_penalty} min={0.5} max={2} step={0.05} rest={1} onChange={(v) => set({ repeat_penalty: v })} />}
          {has('repeat_last_n') && <NumberField label="Repeat last N (tokens)" value={draft.repeat_last_n} placeholder="model default" onChange={(v) => set({ repeat_last_n: v })} />}
          {has('presence_penalty') && <Slider label="Presence penalty" value={draft.presence_penalty} min={-2} max={2} step={0.1} rest={0} onChange={(v) => set({ presence_penalty: v })} />}
          {has('frequency_penalty') && <Slider label="Frequency penalty" value={draft.frequency_penalty} min={-2} max={2} step={0.1} rest={0} onChange={(v) => set({ frequency_penalty: v })} />}
          {has('seed') && <NumberField label="Seed" value={draft.seed} placeholder="by chance" least={0} onChange={(v) => set({ seed: v })} />}
          {sampling && <div className="hint">Sent to the model's server as they are; what is not set is the server's own default. Top K and Min P narrow the choice of the next token to the likeliest ones. The penalties make tokens that already stand in the last N tokens less likely: Repeat penalty above 1 (1 is none), Presence for a token that stands there at all, Frequency the more often it does. The same seed gives the same answer to the same request.</div>}
          {!note && <div className="hint">Only options this model accepts are shown. Saved for this chat.</div>}
        </div>
      )}
    </div>
  )
}
