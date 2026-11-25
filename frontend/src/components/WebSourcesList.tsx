import { visible, webSourcesNote } from '../tree'
import type { WebSourcesBlock } from '../types'

/** Only an http(s) address becomes a link: a result is untrusted, and its address is shown as text otherwise. */
function linkable(url: string): boolean {
  return /^https?:\/\//i.test(url)
}

/** What the web search before this answer gave it, with the words HunterAI searched for. Titles and excerpts are
 * shown as plain text, never rendered. */
export function WebSourcesList({ block }: { block: WebSourcesBlock }) {
  const note = webSourcesNote(block)
  if (note) return <div className="sources-note">{visible(note)}</div>
  return (
    <details className="sources">
      <summary>Web results ({block.items.length})</summary>
      <div className="hint">HunterAI searched the web for <span className="mono">{visible(block.query)}</span> before this answer. A result is a title and a few words of a page, not the page.</div>
      {block.items.map((s) => (
        <div key={s.n} className="source web">
          <b>[w{s.n}]</b> {linkable(s.url)
            ? <a href={s.url} target="_blank" rel="noreferrer noopener">{visible(s.title)}</a>
            : <span>{visible(s.title)}</span>}
          {s.engine && <span className="muted"> · {visible(s.engine)}</span>}
          <div className="mono muted">{visible(s.url)}</div>
          {s.snippet && <div>{visible(s.snippet)}</div>}
        </div>
      ))}
    </details>
  )
}
