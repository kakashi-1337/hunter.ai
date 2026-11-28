import { describe, expect, it } from 'vitest'
import { gib, meterGroups } from '../monitor'
import type { MonitorReading } from '../types'

const GIB = 1024 ** 3
const CARD = { name: 'NVIDIA GeForce RTX 5080 Laptop GPU', load: 32, temp: 50, limit: 85 as number | null, power: 31.2, used: 10.2 * GIB, total: 15.9 * GIB,
  holders: [{ name: 'coli', bytes: 7.68 * GIB }] }
const READING: MonitorReading = {
  cpu: { load: 48.2, temp: 61, limit: 90 },
  ram: { used: 24.9 * GIB, total: 30.2 * GIB, holders: [{ name: 'coli', bytes: 7.5 * GIB }, { name: 'ollama', bytes: 4 * GIB }] },
  cards: [CARD],
}
const rows = (r: MonitorReading, group = 0) => meterGroups(r)[group].meters
const row = (r: MonitorReading, label: string, group = 0) => rows(r, group).find((m) => m.label === label)!

describe('bytes in GiB', () => {
  it('have three figures', () => {
    expect([0.31, 5.26, 24.9, 128].map((n) => gib(n * GIB))).toEqual(['0.31', '5.26', '24.9', '128'])
    expect(gib(0)).toBe('0.00')
  })
})

describe('what the live monitor shows for a reading', () => {
  it('is this machine, then each graphics card under its name', () => {
    expect(meterGroups(READING).map((g) => g.heading)).toEqual(['This machine', 'NVIDIA GeForce RTX 5080 Laptop GPU'])
    expect(rows(READING).map((m) => [m.label, m.figure])).toEqual([['CPU', '48%'], ['CPU temperature', '61°C of 90°C'], ['RAM', '24.9 / 30.2 GiB']])
    expect(rows(READING, 1).map((m) => [m.label, m.figure])).toEqual([['VRAM', '10.2 / 15.9 GiB'], ['GPU load', '32%'], ['Temperature', '50°C of 85°C'], ['Power', '31 W']])
    expect(meterGroups({ ...READING, cards: [] }).map((g) => g.heading)).toEqual(['This machine'])
    expect(meterGroups({ ...READING, cards: [CARD, { ...CARD, name: 'Second' }] }).map((g) => g.heading)).toEqual(['This machine', CARD.name, 'Second'])
  })

  it('fills each bar by the share the figure is of its whole', () => {
    expect(row(READING, 'CPU').share).toBeCloseTo(0.482)
    expect(row(READING, 'CPU temperature').share).toBeCloseTo(61 / 90)
    expect(row(READING, 'RAM').share).toBeCloseTo(24.9 / 30.2)
    expect(row(READING, 'VRAM', 1).share).toBeCloseTo(10.2 / 15.9)
    expect(row(READING, 'GPU load', 1).share).toBeCloseTo(0.32)
    expect(row(READING, 'Power', 1).share).toBeNull()                   // watts have no whole here: a figure without a bar
    expect(row({ ...READING, cpu: { load: 100, temp: 97, limit: 90 } }, 'CPU temperature').share).toBe(1)        // a bar is never longer than its track
  })

  it('names who holds the memory, then what they leave, then what is free', () => {
    expect(row(READING, 'RAM').parts).toEqual([{ name: 'coli', figure: '7.50 GiB' }, { name: 'ollama', figure: '4.00 GiB' },
      { name: 'other', figure: '13.4 GiB' }, { name: 'free', figure: '5.30 GiB' }])
    expect(row(READING, 'VRAM', 1).parts).toEqual([{ name: 'coli', figure: '7.68 GiB' }, { name: 'other', figure: '2.52 GiB' }, { name: 'free', figure: '5.70 GiB' }])
  })

  it('gives an amount under a tenth of a GiB in MiB, where GiB would read 0.00', () => {
    const MIB = 1024 ** 2
    const vram = (holders: { name: string; bytes: number }[]) => row({ ...READING, cards: [{ ...CARD, used: 13.2 * GIB, holders }] }, 'VRAM', 1).parts
    expect(vram([{ name: 'qwen36', bytes: 13.1 * GIB }, { name: 'gnome-shell', bytes: 3.6 * MIB }])).toEqual([
      { name: 'qwen36', figure: '13.1 GiB' }, { name: 'gnome-shell', figure: '4 MiB' }, { name: 'other', figure: '99 MiB' }, { name: 'free', figure: '2.70 GiB' }])
    expect(vram([{ name: 'a', bytes: GIB / 10 }, { name: 'b', bytes: GIB / 10 - MIB }]).slice(0, 2).map((p) => p.figure)).toEqual(['0.10 GiB', '101 MiB'])
    expect(row(READING, 'VRAM', 1).figure).toBe('10.2 / 15.9 GiB')           // the figure beside the name stays in GiB
  })

  it('has no "other" where nobody is named, nor where the named ones hold all that is in use', () => {
    const ram = (holders: { name: string; bytes: number }[]) => row({ ...READING, ram: { used: 8 * GIB, total: 16 * GIB, holders } }, 'RAM').parts.map((p) => p.name)
    expect(ram([])).toEqual(['free'])
    expect(ram([{ name: 'coli', bytes: 8 * GIB }])).toEqual(['coli', 'free'])
    expect(ram([{ name: 'coli', bytes: 9 * GIB }])).toEqual(['coli', 'free'])          // what was counted for it is more than is in use
    expect(row({ ...READING, ram: { used: 17 * GIB, total: 16 * GIB, holders: [] } }, 'RAM').parts).toEqual([{ name: 'free', figure: '0 MiB' }])
  })

  it('marks a temperature near its limit and at it, and nothing else', () => {
    const at = (temp: number, limit: number | null = 85) => row({ ...READING, cards: [{ ...CARD, temp, limit }] }, 'Temperature', 1)
    expect([74, 75, 84, 85, 91].map((t) => at(t).level)).toEqual(['', 'warn', 'warn', 'bad', 'bad'])
    expect(row({ ...READING, cpu: { load: 99, temp: 80, limit: 90 } }, 'CPU temperature').level).toBe('warn')
    expect([at(99, null).level, at(99, null).figure, at(99, null).share]).toEqual(['', '99°C', null])       // no limit is set: nothing to fill a bar against
    expect(meterGroups({ ...READING, cpu: { load: 100, temp: 61, limit: 90 } }).flatMap((g) => g.meters).filter((m) => m.level).length).toBe(0)
  })

  it('leaves out what the machine does not say', () => {
    const bare: MonitorReading = { cpu: { load: null, temp: null, limit: 90 }, ram: null,
      cards: [{ name: 'Second', load: null, temp: null, limit: 85, power: null, used: null, total: 8 * GIB, holders: [] }] }
    expect(rows(bare).map((m) => [m.label, m.figure, m.share])).toEqual([['CPU', '—', null]])
    expect(rows(bare, 1).map((m) => [m.label, m.figure, m.share])).toEqual([['GPU load', '—', null]])
  })

  it('says of each row what its figure is', () => {
    const all = meterGroups(READING).flatMap((g) => g.meters)
    expect(all.length).toBe(7)
    for (const m of all) expect(m.about.length).toBeGreaterThan(20)
    expect(row(READING, 'RAM').about).toContain('files a program has only mapped are not counted')
    expect(row(READING, 'CPU temperature').about).toContain('HUNTERAI_MAX_CPU_TEMP')
    expect(row(READING, 'Temperature', 1).about).toContain('HUNTERAI_MAX_GPU_TEMP')
  })
})
