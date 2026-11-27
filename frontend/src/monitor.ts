import { NEAR_LIMIT } from './tree'
import type { Holder, MonitorReading } from './types'

/** One row of the live monitor: a name and a figure, with a bar where the figure is a share of a whole. */
export interface Meter {
  label: string
  figure: string
  /** How much of its bar is filled, 0 to 1. null: a figure without a bar. */
  share: number | null
  /** Only a temperature has one: near its limit, or at it. */
  level: '' | 'warn' | 'bad'
  /** What the figure is, for the one who points at the row. */
  about: string
  /** For memory: who holds it, largest first, then what the named ones leave, then what is free. */
  parts: { name: string; figure: string }[]
}
export interface MeterGroup { heading: string; meters: Meter[] }

const GIB = 1024 ** 3

/** Bytes in GiB with three figures: "0.31", "24.9", "128". */
export function gib(bytes: number): string {
  const n = bytes / GIB
  return n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)
}

const share = (part: number, whole: number): number => (whole > 0 ? Math.min(1, Math.max(0, part / whole)) : 0)

function load(label: string, percent: number | null, about: string): Meter {
  return { label, figure: percent === null ? '—' : `${Math.round(percent)}%`, share: percent === null ? null : share(percent, 100), level: '', about, parts: [] }
}

function heat(label: string, degrees: number, limit: number | null, setting: string): Meter {
  const about = `Against the limit at which a run waits for it to cool (${setting}).`
  if (limit === null) return { label, figure: `${Math.round(degrees)}°C`, share: null, level: '', about, parts: [] }
  return { label, figure: `${Math.round(degrees)}°C of ${Math.round(limit)}°C`, share: share(degrees, limit),
    level: degrees >= limit ? 'bad' : degrees >= limit - NEAR_LIMIT ? 'warn' : '', about, parts: [] }
}

function held(label: string, used: number, total: number, holders: Holder[], about: string): Meter {
  const parts = holders.map((h) => ({ name: h.name, figure: `${gib(h.bytes)} GiB` }))
  const rest = used - holders.reduce((sum, h) => sum + h.bytes, 0)
  if (holders.length && rest >= GIB / 100) parts.push({ name: 'other', figure: `${gib(rest)} GiB` })
  parts.push({ name: 'free', figure: `${gib(Math.max(0, total - used))} GiB` })
  return { label, figure: `${gib(used)} / ${gib(total)} GiB`, share: share(used, total), level: '', about, parts }
}

/** What the live monitor shows for a reading: this machine's processor and memory, then each graphics card under its
 * name. What the machine did not say has no row, except for a load, which keeps its row with "—". */
export function meterGroups(r: MonitorReading): MeterGroup[] {
  const machine = [load('CPU', r.cpu.load, "The share of the processors' time that was work since the last look.")]
  if (r.cpu.temp !== null) machine.push(heat('CPU temperature', r.cpu.temp, r.cpu.limit, 'HUNTERAI_MAX_CPU_TEMP'))
  if (r.ram) {
    machine.push(held('RAM', r.ram.used, r.ram.total, r.ram.holders,
      'Memory in use: what the system does not call available. Under it, the programs that hold the most for themselves; files a program has only mapped are not counted.'))
  }
  const groups = [{ heading: 'This machine', meters: machine }]
  for (const card of r.cards) {
    const meters: Meter[] = []
    if (card.used !== null && card.total !== null) {
      meters.push(held('VRAM', card.used, card.total, card.holders, "The card's memory in use. Under it, the programs that hold the most of it, as its driver tells it."))
    }
    meters.push(load('GPU load', card.load, 'The share of the last moment in which the card was at work.'))
    if (card.temp !== null) meters.push(heat('Temperature', card.temp, card.limit, 'HUNTERAI_MAX_GPU_TEMP'))
    if (card.power !== null) meters.push({ label: 'Power', figure: `${Math.round(card.power)} W`, share: null, level: '', about: 'What the card draws now, in watts.', parts: [] })
    groups.push({ heading: card.name, meters })
  }
  return groups
}
