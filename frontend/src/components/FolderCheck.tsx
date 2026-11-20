import { useEffect, useState } from 'react'
import * as api from '../api'

const describe = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** The check of the chat's working folder: a command the owner sets for it (the project's tests). In coding mode
 * HunterAI runs it by itself before a run changes anything and after the run, and the answer shows both outcomes.
 * It is kept for the folder, so every chat that works there uses it. */
export function FolderCheck({ chatId, folder }: { chatId: string; folder: string | null }) {
  const [command, setCommand] = useState('')
  const [said, setSaid] = useState<{ text: string; bad: boolean } | null>(null)
  useEffect(() => {
    let wanted = true
    api.getCheck(chatId).then((c) => { if (wanted) setCommand(c.command) }).catch((e) => { if (wanted) setSaid({ text: describe(e), bad: true }) })
    return () => { wanted = false }
  }, [chatId, folder])
  const save = async (text: string) => {
    try {
      const kept = await api.putCheck(chatId, text)
      setCommand(kept.command)
      setSaid({ text: kept.command ? `Saved for ${kept.folder}. Every chat that works in this folder uses it.` : `Removed. No check runs in ${kept.folder}.`, bad: false })
    } catch (e) {
      setSaid({ text: describe(e), bad: true })
    }
  }
  return (
    <div className="field">
      <span>Check command for this folder (optional)</span>
      <div className="row">
        <input className="grow mono" aria-label="Check command" value={command} placeholder="pytest -q" onChange={(e) => setCommand(e.target.value)} />
        <button className="btn" onClick={() => void save(command)}>Save</button>
        <button className="btn" onClick={() => void save('')}>Remove</button>
      </div>
      <span className="hint">The tests of the project, or any command that ends with exit code 0 when all is well. HunterAI runs it in this folder by itself, in coding mode: before a run changes anything, and after the run. The answer then shows whether the run made it pass or fail.</span>
      <span className="hint">It runs whatever the command runs, files the model has just written included, and does not ask. Set it only for a folder where that is what you want.</span>
      {said && <span className={said.bad ? 'ask-warning' : 'hint'} role={said.bad ? 'alert' : 'status'}>{said.text}</span>}
    </div>
  )
}
