import { useState } from 'react'
import { confirmBox } from '../dialog'
import { useStore } from '../store'
import { changesView, checkView, undoQuestion, visible } from '../tree'
import type { ChangesBlock, CheckResult, FolderCheck } from '../types'

/** A list this long is open from the start; a longer one is folded away. */
const OPEN_UP_TO = 12
/** No more rows than this are drawn; the rest are counted. */
const ROWS = 200
const BADGE: Record<ChangesBlock['files'][number]['change'], string> = { new: 'ok', changed: 'warn', deleted: 'bad' }

/** What the check of the folder said before the run and after it, with what it wrote. What a failed check wrote
 * after the run is open from the start: it is what the owner wants to read. */
function RunCheck({ check }: { check: FolderCheck }) {
  const view = checkView(check)
  const wrote = (when: string, r: CheckResult | null, open: boolean) => (r && r.output ? (
    <details open={open}>
      <summary>What the check wrote {when} ({r.seconds} s)</summary>
      <pre>{visible(r.output)}</pre>
    </details>
  ) : null)
  return (
    <div className={`run-check ${view.level}`}>
      <div className="verdict">Check <code className="mono">{visible(check.command)}</code>: {visible(view.text)}</div>
      {wrote('after the run', check.after, !check.after?.passed)}
      {wrote('before the run', check.before, false)}
    </div>
  )
}

/** Under the answer of a run in coding mode: the files it changed in the work folder, and the button that puts
 * them back as they were before it. When no save point could be kept, the reason stands here instead. */
export function RunChanges({ block }: { block: ChangesBlock }) {
  const busy = useStore((s) => s.live !== null)
  const [working, setWorking] = useState(false)
  const view = changesView(block)
  const undo = async () => {
    if (!(await confirmBox(undoQuestion(block)))) return
    setWorking(true)
    try {
      await useStore.getState().undoRun(block.run_id)
    } finally {
      setWorking(false)
    }
  }
  return (
    <div className={`run-changes ${view.level}`} role="note">
      <div className="row">
        <span className="grow">{visible(view.text)}</span>
        {view.canUndo && (
          <button className="btn" disabled={busy || working} title={busy ? 'A run is going: wait for it to end, or stop it.' : undefined}
            onClick={() => void undo()}>{working ? 'Putting back…' : 'Undo these changes'}</button>
        )}
      </div>
      {block.files.length > 0 && (
        <details open={block.files.length <= OPEN_UP_TO && !block.undone && !block.left.length}>
          <summary>{block.files.length === 1 ? 'The file' : `The ${block.files.length} files`}</summary>
          <ul>
            {block.files.slice(0, ROWS).map((f) => (
              <li key={f.path}>
                <span className={`badge ${BADGE[f.change] ?? ''}`}>{f.change}</span>
                <span className="mono">{visible(f.path)}</span>
                {block.left.includes(f.path) && <span className="hint"> left as it is</span>}
              </li>
            ))}
            {block.files.length > ROWS && <li className="hint">and {block.files.length - ROWS} more</li>}
          </ul>
        </details>
      )}
      {block.check && <RunCheck check={block.check} />}
    </div>
  )
}
