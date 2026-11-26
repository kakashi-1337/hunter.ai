import { useState } from 'react'
import { llamaModelNote, type LlamaFindView } from '../tree'
import { copyText } from './Markdown'

/** A command to copy: its text in a box of its own, whole, and a button that copies it. */
function Command({ text }: { text: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = async () => {
    setState((await copyText(text)) ? 'copied' : 'failed')
    setTimeout(() => setState('idle'), 1500)
  }
  return (
    <div className="commandbox">
      <pre className="command mono">{text}</pre>
      <button className="btn" onClick={() => void copy()}>{state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}</button>
    </div>
  )
}

/** What Settings shows to start a llama-server on this machine (tree.llamaFindView): what was found, a model of
 * Ollama to choose with the command that starts llama-server with it, the command for a file of the owner's own, and
 * a button that saves the address the server will answer at. `onUse` saves an address and rejects with the reason. */
export function LlamaStart({ view, onUse }: { view: LlamaFindView; onUse(address: string): Promise<unknown> }) {
  const [picked, setPicked] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // The one that was picked; before that the first that can be started, which is the local model in use when it can.
  const model = view.models.find((m) => m.name === picked) ?? view.models.find((m) => m.command) ?? view.models[0]
  const note = model ? llamaModelNote(model) : null
  const save = async (address: string) => {
    setBusy(true)
    setError('')
    try {
      await onUse(address)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="llamastart">
      {view.lines.map((line) => <div key={line} className="hint" role="status">{line}</div>)}
      {view.warn && <div className="ask-warning" role="alert">{view.warn}</div>}
      {model && note && (
        <>
          <label className="field">
            <span>Model of Ollama</span>
            <select aria-label="Model of Ollama to start llama-server with" value={model.name} onChange={(e) => setPicked(e.target.value)}>
              {view.models.map((m) => <option key={m.name} value={m.name}>{m.name}{m.command ? '' : ' (cannot be started this way)'}</option>)}
            </select>
          </label>
          {model.command && <Command key={model.name} text={model.command} />}
          {note.warn && <div className="ask-warning" role="alert">{note.warn}</div>}
          {note.lines.map((line) => <div key={line} className="hint">{line}</div>)}
        </>
      )}
      {view.plain && <div className="hint">For a GGUF file of your own, with its path in place of your-model.gguf:</div>}
      {view.plain && <Command text={view.plain} />}
      {view.after.map((line) => <div key={line} className="hint">{line}</div>)}
      {view.use && <div className="row"><button className="btn" disabled={busy} onClick={() => void save(view.use!)}>{busy ? 'Saving…' : `Use ${view.use}`}</button></div>}
      {error && <div className="ask-warning" role="alert">{error}</div>}
    </div>
  )
}
