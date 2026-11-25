import { describe, expect, it } from 'vitest'
import { retrievalLine, webSourcesNote, webSourcesOf } from '../tree'
import type { Message, WebSourcesBlock } from '../types'

const block = (p: Partial<WebSourcesBlock> = {}): WebSourcesBlock => ({ type: 'web_sources', query: 'node.js security release', items: [], results: 0, error: null, ...p })
const item = (n: number) => ({ n, title: `t${n}`, url: `https://x.example/${n}`, snippet: 's', engine: 'duckduckgo' })
const msg = (blocks: Message['blocks']) => ({ blocks } as Message)

describe('what a web search before the answer gave it', () => {
  it('is found among the blocks of an answer, or is not there', () => {
    const b = block({ items: [item(1)], results: 1 })
    expect(webSourcesOf(msg([{ type: 'text', text: 'a' }, b]))).toBe(b)
    expect(webSourcesOf(msg([{ type: 'text', text: 'a' }]))).toBeNull()
  })
  it('says why there is nothing to show: the search failed, found nothing, or nothing of it fit', () => {
    expect(webSourcesNote(block({ error: 'SearXNG is not running at http://127.0.0.1:8888.' })))
      .toBe('Web search for "node.js security release" failed: SearXNG is not running at http://127.0.0.1:8888.')
    expect(webSourcesNote(block())).toBe('Web search for "node.js security release" found nothing.')
    expect(webSourcesNote(block({ results: 4 }))).toBe('Web search for "node.js security release" found 4 results, but none fit this chat\'s context window.')
    expect(webSourcesNote(block({ items: [item(1)], results: 1 }))).toBeNull()
  })
})

describe('the line of a lookup step in the trajectory', () => {
  it('counts chunks for the knowledge base, notes for memory, and results with their engines for the web', () => {
    expect(retrievalLine('kb_search', { used: 3, hits: 8 })).toBe('3 of 8 chunks used')
    expect(retrievalLine('memory', { used: 1, hits: 5 })).toBe('1 of 5 notes used')
    expect(retrievalLine('web_search', { used: 2, results: 5, engines: ['brave', 'duckduckgo'] })).toBe('2 of 5 results used · brave, duckduckgo')
    expect(retrievalLine('web_search', { used: 0, results: 0 })).toBe('0 of 0 results used')
    expect(retrievalLine('kb_search', {})).toBe('0 of 0 chunks used')
  })
  it('says when only the keyword index answered a knowledge search', () => {
    expect(retrievalLine('kb_search', { used: 1, hits: 1, keywords_only: 'embedder busy' })).toBe('1 of 1 chunks used · keyword index only (embedder busy)')
  })
})
