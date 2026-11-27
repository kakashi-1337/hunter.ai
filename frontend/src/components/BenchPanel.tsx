import { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from '../api'
import { benchLine, benchTable, checkLine, checkScore, cloudLine, differs, handChoice, judgeLine, judgingLine, modelNames, progressLine, seconds, sharedLine, sizeLine, stateWord, verdictLine, wrongLine, wrongOnes } from '../bench'
import { confirmBox } from '../dialog'
import { useStore } from '../store'
import { visible } from '../tree'
import type { BenchDetail, BenchListed, BenchResult, BenchRun, BenchSet, CheckedAnswer, JudgeCheck, ModelRef, SystemStatus } from '../types'
import { Modal } from './Modal'

const POLL_MS = 2000
const keyOf = (ref: ModelRef) => `${ref.provider}\u0000${ref.model}`
const nameOf = (ref: ModelRef) => `${ref.provider}/${ref.model}`
const said = (e: unknown) => (e instanceof Error ? e.message : String(e))
const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`

/** The runs of one case on one model: for each repeat the mark, the lines of the rules that did not hold, the
 * calls and the answer, so a mark can be seen for what it is. A mark the rules got wrong can be set right here:
 * `onMarked` is called after it, for the table to be counted again. `rule`: how the test is judged, in its own
 * words, for the owner who decides: as the bench kept them, or as the set says them now for a bench from before. */
function CellDetail({ run, model, name, caseId, made, rule, onMarked }: {
  run: BenchRun; model: ModelRef; name: string; caseId: string; made: number; rule: string | null; onMarked: () => void
}) {
  const [results, setResults] = useState<BenchResult[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { provider, model: id } = model
  // Asked again when another run of this cell has been made: `made` counts them. Another cell is another mount.
  useEffect(() => {
    let stop = false
    api.benchCell(run.id, { provider, model: id }, caseId)
      .then((got) => { if (!stop) { setResults(got); setError(null) } }).catch((e) => { if (!stop) setError(said(e)) })
    return () => { stop = true }
  }, [run.id, provider, id, caseId, made])
  const prompt = run.cases.find((c) => c.id === caseId)?.prompt ?? ''
  const mark = async (result: BenchResult, status: 'pass' | 'fail' | null) => {
    try {
      const now = await api.setBenchMark(result.id, status)
      setResults((have) => have && have.map((r) => (r.id === now.id ? now : r)))
      setError(null)
      onMarked()
    } catch (e) { setError(said(e)) }
  }
  return (
    <div className="bench-detail">
      <h3 className="section">{caseId} on {name}</h3>
      <div className="hint">{visible(prompt)}</div>
      {rule && <div className="bench-rule">How this test is judged: {visible(rule)}</div>}
      {error && <div className="bench-error">{error}</div>}
      {results && results.length === 0 && <div className="hint">This case has not run on this model yet.</div>}
      {results?.map((r) => {
        const choice = handChoice(r)
        const judged = judgeLine(r)
        return (
        <div className="bench-run" key={r.id}>
          <div className="row">
            <span className="bench-verdict">Repeat {r.repeat}: {verdictLine(r)}</span>
            {r.status !== 'skipped' && <span className="hint">{plural(r.turns, 'model turn')}, {seconds(r.duration_ms)}, {r.input_tokens.toLocaleString()} tokens in, {r.output_tokens.toLocaleString()} out</span>}
            {choice && <button className="link bench-by-hand" onClick={() => void mark(r, choice.status)}
              title={choice.status ? 'The rules of a set are lists of phrases and can misread an answer. Your mark is counted, and what the rules said stays.' : 'Take your mark back: the run counts as the rules marked it.'}>{choice.label}</button>}
          </div>
          {judged && <div className={`bench-judge${judged.differs ? ' differs' : ''}`}>{visible(judged.text)}</div>}
          {r.reasons.length > 0 && <ul className="bench-reasons">{r.reasons.map((line, i) => <li key={i}>{visible(line)}</li>)}</ul>}
          {r.calls.length > 0 && <ul className="bench-calls">{r.calls.map((c, i) => (
            <li className="mono" key={i}>{visible(c.name)} {visible(c.arguments)}{c.problem ? ` (not run: ${visible(c.problem)})` : ''}</li>))}</ul>}
          {r.calls_made > r.calls.length && <div className="hint">{r.calls.length === 1 ? 'The first call' : `The first ${r.calls.length} calls`} of {r.calls_made} {r.calls.length === 1 ? 'is' : 'are'} shown.</div>}
          {r.status !== 'skipped' && (r.answer ? <pre>{visible(r.answer)}</pre> : <div className="hint">No answer.</div>)}
          {r.thinking && (
            <details className="bench-thinking" open={!r.answer}>
              <summary>The end of what it thought last</summary>
              <pre>{visible(r.thinking)}</pre>
            </details>)}
        </div>
        )
      })}
    </div>
  )
}

/** The same scripted tests for every model: pick models and a set, run them, and read the table. No tool is run in
 * a bench: every call a model makes is answered from the script of its case. */
export function BenchPanel({ onClose }: { onClose: () => void }) {
  const providers = useStore((s) => s.providers)
  const refreshProviders = useStore((s) => s.refreshProviders)
  const [sets, setSets] = useState<BenchSet[]>([])
  const [runs, setRuns] = useState<BenchListed[]>([])
  const [picked, setPicked] = useState<string[]>([])
  const [setId, setSetId] = useState('')
  const [repeats, setRepeats] = useState(3)
  const [opened, setOpened] = useState<BenchDetail | null>(null)
  const [cell, setCell] = useState<{ model: ModelRef; caseId: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [machine, setMachine] = useState<SystemStatus | null>(null)       // what holds a model in memory now
  const [thinkingOff, setThinkingOff] = useState(false)                   // every call is asked without thinking
  const [judge, setJudge] = useState('')                                  // the model that reads the answers of a bench that has ended
  const [checks, setChecks] = useState<JudgeCheck[]>([])                  // how judge models did on the known answers of a set
  const [wrong, setWrong] = useState<{ id: string; answers: CheckedAnswer[] } | null>(null)      // what one check did not get right

  const offered = useMemo(() => providers.filter((p) => p.configured && p.models.length > 0), [providers])
  const models: ModelRef[] = offered.flatMap((p) => p.models.map((m) => ({ provider: p.name, model: m.id }))).filter((ref) => picked.includes(keyOf(ref)))
  const set = sets.find((s) => s.id === setId) ?? null       // never one that cannot be used: such a set cannot be picked
  const reading = opened?.judging?.status === 'running'      // a judge reads the open bench now
  const all: ModelRef[] = offered.flatMap((p) => p.models.map((m) => ({ provider: p.name, model: m.id })))
  const judgeRef = all.find((ref) => nameOf(ref) === judge) ?? null
  // The checks of judges on the set of the open bench: the one that runs now, and the one of the judge that is picked.
  const benchSet = opened?.run.set_id
  const ofSet = checks.filter((c) => c.set_id === benchSet)
  const checkOf = (ref: ModelRef | null) => (ref ? ofSet.find((c) => c.provider === ref.provider && c.model === ref.model) ?? null : null)
  const runningCheck = ofSet.find((c) => c.status === 'running') ?? null
  const shownCheck = runningCheck ?? checkOf(judgeRef)
  const known = sets.find((s) => s.id === benchSet)?.known ?? 0
  const live = runs.some((r) => r.run.status === 'running') || opened?.run.status === 'running' || reading || runningCheck !== null
  const oneAtATime = runningCheck ? 'A judge is being checked: one at a time' : reading ? 'A judge is reading a bench: one at a time'
    : live ? 'A bench is running: one at a time' : undefined
  const cloud = cloudLine(models)
  const shared = sharedLine(models, machine)
  const sharedJudge = sharedLine(judgeRef ? [judgeRef] : [], machine, true)

  const open = useCallback(async (id: string) => {
    try { setOpened(await api.benchRun(id)) } catch (e) { setError(said(e)) }
  }, [])
  const list = async () => {
    const got = await api.benchRuns()
    setRuns(got)
    return got
  }
  const listChecks = useCallback(async (setId: string) => {
    try { setChecks(await api.judgeChecks(setId)) } catch { /* without them the pick has no numbers, and that is all */ }
  }, [])

  // Asked when the panel opens and again whenever the pick changes: what is held in memory changes while it is open.
  const pick = picked.join('\n')
  useEffect(() => {
    let stop = false
    api.systemStatus().then((got) => { if (!stop) setMachine(got) }).catch(() => {})       // without it there is no warning, and that is all
    return () => { stop = true }
  }, [pick, judge])

  useEffect(() => {
    let stop = false
    void refreshProviders().catch(() => {})                // a model pulled since the page was loaded is offered too
    api.benchSets().then((got) => {
      if (stop) return
      setSets(got)
      setSetId((now) => now || (got.find((s) => !s.problem)?.id ?? ''))
    }).catch((e) => { if (!stop) setError(said(e)) })
    api.benchRuns().then((got) => {
      if (stop) return
      setRuns(got)
      if (got.length) void open(got[0].run.id)             // the newest, which is the running one when one runs
    }).catch((e) => { if (!stop) setError(said(e)) })
    return () => { stop = true }
  }, [open, refreshProviders])

  // While the opened bench runs, ask again every two seconds; when it has ended, the list learns its end too.
  const openedId = opened?.run.id, openedStatus = opened?.run.status
  useEffect(() => {
    if (!opened || (openedStatus !== 'running' && !reading)) return
    const timer = setTimeout(() => void open(opened.run.id), POLL_MS)
    return () => clearTimeout(timer)
  }, [opened, openedStatus, reading, open])
  useEffect(() => {
    if (!openedId || !openedStatus || openedStatus === 'running') return
    let stop = false
    api.benchRuns().then((got) => { if (!stop) setRuns(got) }).catch(() => {})
    return () => { stop = true }
  }, [openedId, openedStatus])

  // The checks of judges on the set of the bench that is open; while one of them runs, asked for again every two seconds.
  useEffect(() => {
    if (!benchSet) return
    let stop = false
    api.judgeChecks(benchSet).then((got) => { if (!stop) setChecks(got) }).catch(() => {})     // without them the pick has no numbers, and that is all
    return () => { stop = true }
  }, [benchSet])
  useEffect(() => {
    if (!runningCheck || !benchSet) return
    const timer = setTimeout(() => void listChecks(benchSet), POLL_MS)
    return () => clearTimeout(timer)
  }, [runningCheck, benchSet, listChecks])
  // The known answers the shown check did not get right, once it is over and there are any.
  const shownId = shownCheck?.id
  const hasWrong = shownCheck != null && shownCheck.status !== 'running' && shownCheck.done > shownCheck.correct
  useEffect(() => {
    if (!shownId || !hasWrong) return
    let stop = false
    api.judgeCheck(shownId).then((got) => { if (!stop) setWrong({ id: shownId, answers: wrongOnes(got.answers) }) }).catch(() => {})
    return () => { stop = true }
  }, [shownId, hasWrong])
  const missed = hasWrong && wrong && wrong.id === shownId ? wrong.answers : []

  const act = async (what: () => Promise<void>) => {
    setError(null)
    setBusy(true)
    try { await what() } catch (e) { setError(said(e)) } finally { setBusy(false) }
  }
  const start = () => act(async () => {
    const started = await api.startBench(models, setId, repeats, thinkingOff)
    setCell(null)
    await open(started.id)
    await list()
  })
  const startJudging = (id: string) => act(async () => {
    if (!judgeRef) return
    await api.judgeBench(id, judgeRef)
    await open(id)
  })
  const stopJudging = (id: string) => act(async () => {
    await api.cancelJudging(id)
    await open(id)
  })
  const startCheck = () => act(async () => {
    if (!judgeRef || !benchSet) return
    await api.checkJudge(judgeRef, benchSet)
    await listChecks(benchSet)
  })
  const stopCheck = (id: string) => act(async () => {
    await api.cancelJudgeCheck(id)
    if (benchSet) await listChecks(benchSet)
  })
  const cancel = (id: string) => act(async () => {
    await api.cancelBench(id)
    await open(id)
  })
  const remove = (run: BenchRun) => act(async () => {
    if (!(await confirmBox(`Delete this bench of ${run.set_name} and its results?`))) return
    await api.deleteBench(run.id)
    const left = await list()
    if (opened?.run.id === run.id) {
      setCell(null)
      setOpened(null)
      if (left.length) await open(left[0].run.id)
    }
  })
  const again = (run: BenchRun) => {
    setPicked(run.models.map(keyOf))
    if (sets.some((s) => s.id === run.set_id && !s.problem)) setSetId(run.set_id)
    setRepeats(run.repeats)
    setThinkingOff(run.thinking_off === true)
  }
  const toggle = (ref: ModelRef) => setPicked((now) => (now.includes(keyOf(ref)) ? now.filter((k) => k !== keyOf(ref)) : [...now, keyOf(ref)]))

  const run = opened?.run ?? null
  const names = run ? modelNames(run.models) : []
  const table = opened ? benchTable(opened.run, opened.marks) : null
  const shownCell = cell && run && run.models.some((m) => keyOf(m) === keyOf(cell.model)) ? cell : null

  return (
    <Modal wide title="Bench" onClose={onClose} footer={<>
      <span className="hint grow">The same tests for every model. No tool is run: each call is answered from the script of its test.</span>
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      {error && <div className="bench-error">{error}</div>}

      <div className="bench-models" role="group" aria-label="Models">
        {offered.length === 0 && <div className="hint">No model is set up yet. Add one in Settings.</div>}
        {offered.map((p) => (
          <div key={p.name}>
            <div className="provider">{p.name}</div>
            {p.models.map((m) => {
              const ref = { provider: p.name, model: m.id }
              return (
                <label className="check" key={m.id}>
                  <input type="checkbox" aria-label={`${p.name}/${m.id}`} checked={picked.includes(keyOf(ref))} onChange={() => toggle(ref)} />
                  <span>{m.label === m.id ? m.id : `${m.label} (${m.id})`}</span>
                </label>
              )
            })}
          </div>
        ))}
      </div>
      <div className="row">
        <select aria-label="Set" value={setId} onChange={(e) => setSetId(e.target.value)}>
          {sets.map((s) => (
            <option key={s.id} value={s.id} disabled={!!s.problem} title={s.problem ?? undefined}>
              {s.name} ({s.problem ? 'cannot be used' : plural(s.cases.length, 'case')})
            </option>
          ))}
        </select>
        <select aria-label="Repeats" value={repeats} onChange={(e) => setRepeats(Number(e.target.value))}>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{plural(n, 'repeat')}</option>)}
        </select>
        <label className="check" title="Every call of the bench is asked without thinking: faster, and the same for every model. Unticked, each model is asked as it is: one that thinks by itself thinks, and a turn that ends with thinking only is asked once more without.">
          <input type="checkbox" aria-label="Thinking off" checked={thinkingOff} onChange={(e) => setThinkingOff(e.target.checked)} />
          <span>Thinking off</span>
        </label>
        <span className="bench-size hint grow">{set ? sizeLine(models.length, set.cases.length, repeats) : ''}</span>
        <button className="btn primary" disabled={busy || live || !models.length || !set} onClick={() => void start()}
          title={oneAtATime}>Run</button>
      </div>
      {cloud && <div className="bench-cloud">{cloud}</div>}
      {shared && <div className="bench-shared">{shared}</div>}
      <div className="hint">A chat on a local model during a bench makes both slow. Your own sets are the .toml files in the bench folder of HunterAI&apos;s data folder.</div>

      <h3 className="section">Benches</h3>
      {runs.length === 0 && <div className="hint">No bench has been run yet.</div>}
      <ul className="bench-list">
        {runs.map(({ run: r }) => (
          <li key={r.id} className={r.id === run?.id ? 'on' : undefined}>
            <span className="grow">{new Date(r.created_at).toLocaleString()} · {r.set_name} · {plural(r.models.length, 'model')} · {stateWord(r.status)}</span>
            <button className="btn" onClick={() => { setCell(null); void open(r.id) }}>Open</button>
            <button className="btn danger" disabled={busy || r.status === 'running'} onClick={() => void remove(r)}>Delete</button>
          </li>
        ))}
      </ul>

      {run && table && (
        <div className="bench-opened">
          <div className="row">
            <h3 className="section grow">{run.set_name} · {new Date(run.created_at).toLocaleString()}</h3>
            {run.status === 'running'
              ? <button className="btn danger" disabled={busy} onClick={() => void cancel(run.id)}>Cancel bench</button>
              : <button className="btn" onClick={() => again(run)} title="Tick these models, this set and these repeats above">Pick these again</button>}
          </div>
          <div className="hint">{benchLine(run, opened!.marks.length)}</div>
          {run.note && <div className="bench-note">{run.note}</div>}
          {opened?.progress && <div className="bench-progress">{progressLine(opened.progress, run)}</div>}
          {run.status !== 'running' && (
            <div className="row">
              <select aria-label="Judge" value={judge} disabled={reading || runningCheck !== null} onChange={(e) => setJudge(e.target.value)}
                title="A model that reads the answers of this bench, one after another, now that the bench has ended. It changes no mark: where it and a mark differ, the cell says that there is something to check. A model that was checked on this set has its score beside its name.">
                <option value="">Judge model…</option>
                {all.map((ref) => {
                  const of = checkOf(ref), score = of ? checkScore(of) : null
                  return <option key={keyOf(ref)} value={nameOf(ref)}>{`${nameOf(ref)}${score ? ` · ${score} right` : ''}`}</option>
                })}
              </select>
              {reading
                ? <button className="btn danger" disabled={busy} onClick={() => void stopJudging(run.id)}>Cancel judging</button>
                : <button className="btn" disabled={busy || live || !judgeRef} onClick={() => void startJudging(run.id)}
                    title={oneAtATime}>Judge this bench</button>}
              {runningCheck
                ? <button className="btn danger" disabled={busy} onClick={() => void stopCheck(runningCheck.id)}>Cancel check</button>
                : <button className="btn" disabled={busy || live || !judgeRef || !known} onClick={() => void startCheck()}
                    title={!known ? 'This set has no answers whose right verdict is known: a test gets them as [[case.known]].'
                      : oneAtATime ?? 'Asks this model about the answers of this set whose right verdict is known, to see how often it is right before you let it read a bench.'}>Check this judge</button>}
            </div>)}
          {run.status !== 'running' && sharedJudge && <div className="bench-shared judge">{sharedJudge}</div>}
          {run.status !== 'running' && shownCheck && <div className="bench-check">{checkLine(shownCheck)}</div>}
          {run.status !== 'running' && missed.length > 0 && (
            <details className="bench-check-wrong">
              <summary>{`The ${plural(missed.length, 'known answer')} it did not get right`}</summary>
              <ul>{missed.map((a) => <li key={`${a.case_id} ${a.n}`}><div>{visible(wrongLine(a))}</div><pre>{visible(a.answer)}</pre></li>)}</ul>
            </details>)}
          {opened?.judging && <div className="bench-judging">{judgingLine(opened.judging, opened.marks.filter(differs).length)}</div>}
          <table className="bench-table">
            <thead><tr><th></th>{run.models.map((m, i) => <th key={keyOf(m)} title={`${m.provider}/${m.model}`}>{names[i]}</th>)}</tr></thead>
            <tbody>
              {table.groups.flatMap((g) => [
                <tr className="bench-group" key={g.group}><th>{g.label}</th>{g.totals.map((c, i) => <td key={i}><span className={`bench-cell ${c.tone}`}>{c.text}</span></td>)}</tr>,
                ...g.rows.map((row) => (
                  <tr key={`${g.group}/${row.caseId}`}>
                    <th title={row.prompt}>{row.caseId}</th>
                    {row.cells.map((c, i) => (
                      <td key={i}>
                        <button className={`bench-cell ${c.tone}`} aria-label={`${row.caseId} on ${names[i]}: ${c.text}`}
                          aria-pressed={shownCell?.caseId === row.caseId && keyOf(shownCell.model) === keyOf(run.models[i])}
                          onClick={() => setCell({ model: run.models[i], caseId: row.caseId })}>{c.text}</button>
                      </td>
                    ))}
                  </tr>
                )),
              ])}
              <tr className="bench-all"><th>All</th>{table.totals.map((c, i) => <td key={i}><span className={`bench-cell ${c.tone}`}>{c.text}</span></td>)}</tr>
              <tr className="bench-all"><th>Time</th>{table.times.map((t, i) => <td key={i}>{t}</td>)}</tr>
            </tbody>
          </table>
          {!shownCell && <div className="hint">Press a cell to see its runs: what the model called, what it answered, and which rule did not hold. A mark the rules got wrong can be set right there.</div>}
          {shownCell && <CellDetail key={`${run.id}/${keyOf(shownCell.model)}/${shownCell.caseId}`} run={run} model={shownCell.model} caseId={shownCell.caseId}
            name={names[run.models.findIndex((m) => keyOf(m) === keyOf(shownCell.model))]}
            made={opened!.marks.filter((m) => m.case_id === shownCell.caseId && m.provider === shownCell.model.provider && m.model === shownCell.model.model).length}
            rule={run.cases.find((c) => c.id === shownCell.caseId)?.judge
              ?? sets.find((s) => s.id === run.set_id)?.cases.find((c) => c.id === shownCell.caseId)?.judge ?? null}
            onMarked={() => void open(run.id)} />}
        </div>
      )}
    </Modal>
  )
}
