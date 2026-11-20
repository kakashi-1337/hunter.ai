// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { SourcesList } from '../components/SourcesList'
import type { Source, SourcesBlock } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const source = (n: number, extra: Partial<Source> = {}): Source => ({
  n, chunk_id: `c${n}`, document_id: 'd', collection_id: 'col', source_uri: `/notes/n${n}.md`, ordinal: 0, line_start: 1, line_end: 3,
  score: 0.0164, text: `text of ${n}`, ...extra,
})

describe('the sources under an answer', () => {
  let host: HTMLDivElement
  let root: Root
  beforeEach(() => { host = document.createElement('div'); document.body.append(host); root = createRoot(host) })
  afterEach(() => { act(() => root.unmount()); host.remove() })

  it('say which passage stood first for the exact spelling of an identifier, where a bare score would only look odd', () => {
    const block: SourcesBlock = { type: 'sources', query: 'where is wait_for used', hits: 2, error: null,
      items: [source(1, { exact: true, score: 1.0164 }), source(2)] }
    act(() => { root.render(createElement(SourcesList, { block, answer: 'It is used in the table reader [1].' })) })
    const rows = [...host.querySelectorAll('details.source > summary')].map((s) => s.textContent ?? '')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain('exact spelling')
    expect(rows[0]).not.toContain('1.0164')         // the added 1 is how it is put first, not a measure of anything
    expect(rows[1]).not.toContain('exact spelling')
    expect(rows[1]).toContain('0.0164')
  })
})
