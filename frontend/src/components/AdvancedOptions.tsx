import { useEffect, useRef, useState } from 'react'
import { useChatView, useStore } from '../store'
import { pruneOptions } from '../tree'
import type { Options } from '../types'
import { useModelInfo } from './ModelPicker'

const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const
const VERBOSITY = ['low', 'medium', 'high'] as const

function Slider({ label, value, min, max, step, onChange }: { label: string; value: number | undefined; min: number; max: number; step: number; onChange: (v: number | undefined) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <div className="range">
        <input type="range" min={min} max={max} step={step} value={value ?? min} onChange={(e) => onChange(Number(e.target.value))} />
        <output>{value ?? '—'}</output>
        <button className="icon" title="Use the model's default" disabled={value === undefined} onClick={(e) => { e.preventDefault(); onChange(undefined) }}>reset</button>
      </div>
    </label>
  )
}

function NumberField({ label, value, placeholder, onChange }: { label: string; value: number | undefined; placeholder: string; onChange: (v: number | undefined) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input type="number" min={1} step={1} value={value ?? ''} placeholder={placeholder}
        onChange={(e) => { const n = Math.floor(Number(e.target.value)); onChange(e.target.value === '' || !Number.isFinite(n) || n < 1 ? undefined : n) }} />
    </label>
  )
}

export function AdvancedOptions() {
  const chat = useChatView()
  const patchChat = useStore((s) => s.patchChat)
  const info = useModelInfo()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<Options>({})
  const root = useRef<HTMLDivElement>(null)
  const allowed = info?.options ?? []
  const has = (k: string) => allowed.includes(k)

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

  return (
    <div className="popover" ref={root}>
      <button className="btn" aria-expanded={open} onClick={() => setOpen((o) => !o)}>Options{active ? ` · ${active}` : ''}</button>
      {open && (
        <div className="sheet" role="dialog" aria-label="Model options">
          {allowed.length === 0 && <div className="hint">This model has no adjustable options.</div>}
          {has('temperature') && <Slider label="Temperature" value={draft.temperature} min={0} max={2} step={0.1} onChange={(v) => set({ temperature: v })} />}
          {has('top_p') && <Slider label="Top P" value={draft.top_p} min={0} max={1} step={0.05} onChange={(v) => set({ top_p: v })} />}
          {has('max_tokens') && <NumberField label="Max output tokens" value={draft.max_tokens} placeholder="model default" onChange={(v) => set({ max_tokens: v })} />}
          {has('context_window') && <NumberField label="Context window (tokens)" value={draft.context_window} placeholder={info?.context_window ? String(info.context_window) : 'model default'} onChange={(v) => set({ context_window: v })} />}
          {has('effort') && (
            <label className="field"><span>Reasoning effort</span>
              <select value={draft.effort ?? ''} onChange={(e) => set({ effort: (e.target.value || undefined) as Options['effort'] })}>
                <option value="">model default</option>
                {EFFORTS.map((x) => <option key={x} value={x}>{x}</option>)}
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
          <div className="hint">Only options this model accepts are shown. Saved for this chat.</div>
        </div>
      )}
    </div>
  )
}
