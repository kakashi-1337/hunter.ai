import { useEffect, useState } from 'react'
import * as api from '../api'
import { useSettled } from '../settled'
import { useStore } from '../store'
import { goalLine, goalMark, helpers, visible } from '../tree'
import type { Goal } from '../types'

const STATE = { queued: 'waiting', running: 'working', done: 'finished', error: 'failed', cancelled: 'stopped' } as const

/** One item of the record with what the check found about it: the mark in a few words, and under it what the mark
 * rests on (the words of a tool result, or why there is no verdict). */
function Item({ goal, item, done }: { goal: Goal; item: string; done: boolean }) {
  const mark = goalMark(goal, item, done)
  return (<>
    <div className={`goal-item${done ? ' done' : ''}`}>
      <span className="box" aria-label={done ? 'done' : 'open'}>{done ? '☑' : '☐'}</span>
      <span className="grow goal-text">{visible(item)}</span>
      {mark && <span className={`badge ${mark.level}`}>{mark.label}</span>}
    </div>
    {mark && <div className="goal-proof">{visible(mark.detail)}</div>}
  </>)
}

/** The chat's goal record, in view above the messages for as long as there is one: what is open, what is done,
 * and the subagents of the run on screen. It appears by itself when the orchestrator sets a goal. All text is plain. */
export function GoalStrip() {
  const chatId = useStore((s) => s.chat?.id || undefined)
  const steps = useStore((s) => s.live?.steps ?? s.runDetail?.steps)
  const busy = useStore((s) => s.live !== null)
  const count = useSettled(steps?.length ?? 0)      // the steps a replay brings within a moment ask for the goal once
  const [goal, setGoal] = useState<Goal | null>(null)
  const [open, setOpen] = useState(() => { try { return localStorage.getItem('goal-strip') !== 'closed' } catch { return true } })
  useEffect(() => {
    if (!chatId) { setGoal(null); return }
    let stop = false
    api.getGoal(chatId).then((g) => { if (!stop) setGoal(g) }).catch(() => {})
    return () => { stop = true }
  }, [chatId, count, busy])       // asked again as the run makes progress: the focus tool changes it mid-run
  useEffect(() => { try { localStorage.setItem('goal-strip', open ? 'open' : 'closed') } catch { /* private mode */ } }, [open])
  if (!goal) return null
  const list = helpers(steps ?? [])
  const done = goal.done_items ?? []
  return (
    <section className="goal-strip" aria-label="Goal of this chat">
      <button className="goal-head" aria-expanded={open} onClick={() => setOpen((o) => !o)} title={open ? 'Fold' : 'Unfold'}>
        <span className="k">Goal</span>
        <span className="grow goal-objective-line">{visible(goal.objective)}</span>
        <span className="muted">{goalLine(goal, list)}</span>
        <span className="muted">{open ? '▾' : '▸'}</span>
      </button>
      {open && (<div className="goal-body">
        {goal.open_items.length === 0 && done.length === 0 && <div className="hint">The agent has listed no steps yet.</div>}
        {goal.open_items.map((item, i) => <Item key={`o${i}`} goal={goal} item={item} done={false} />)}
        {done.map((item, i) => <Item key={`d${i}`} goal={goal} item={item} done />)}
        {list.length > 0 && (<>
          <div className="k">Subagents of this run</div>
          {list.map((h) => (
            <div key={h.id} className="goal-item">
              <span className={`dot ${h.state === 'queued' ? '' : h.state}`} title={STATE[h.state]} />
              <span className="grow goal-brief" title={visible(h.brief)}>{visible(h.brief)}</span>
              <span className={`badge ${h.verified ? 'ok' : h.state === 'error' ? 'bad' : h.verified === false ? 'warn' : ''}`}>{h.verified ? 'verified' : h.verified === false && h.state === 'done' ? 'not verified' : STATE[h.state]}</span>
            </div>
          ))}
        </>)}
        {goal.criteria.length > 0 && <details><summary>Success criteria ({goal.criteria.length})</summary><ul>{goal.criteria.map((c, i) => <li key={i}>{visible(c)}</li>)}</ul></details>}
        {(done.length > 0 || Object.keys(goal.marks ?? {}).length > 0) && <div className="hint">An item is marked done only when a tool result of this chat shows the work: the decision model reads the results, and the words it quotes have to stand in one of them. An item the agent only says is done stays open. It is a model's reading of the results, not proof.</div>}
      </div>)}
    </section>
  )
}
