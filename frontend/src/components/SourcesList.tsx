import { sourceLabel } from '../kb'
import { citedNumbers, sourcesNote, unknownCitations, visible } from '../tree'
import type { SourcesBlock } from '../types'

/** What the knowledge base gave this answer. Chunk text is shown as plain text, never rendered. */
export function SourcesList({ block, answer }: { block: SourcesBlock; answer: string }) {
  const note = sourcesNote(block)
  if (note) return <div className="sources-note">{note}</div>
  const cited = citedNumbers(answer)
  const unknown = unknownCitations(answer, block)
  return (
    <details className="sources" open={unknown.length > 0 || undefined}>
      <summary>Sources ({block.items.length})</summary>
      {unknown.length > 0 && <div className="warn-line">The answer cites {unknown.map((n) => `[${n}]`).join(', ')}, but only sources 1 to {block.items.length} were given to the model. Treat those statements as unsupported.</div>}
      {block.items.map((s) => (
        <details key={s.n} className="source">
          <summary>
            <b>[{s.n}]</b> <span className="mono">{visible(sourceLabel(s.source_uri, s.line_start, s.line_end))}</span>
            <span className="muted"> · {s.score.toFixed(4)}</span>
            {cited.has(s.n) && <span className="badge">cited</span>}
          </summary>
          <pre>{visible(s.text)}</pre>
        </details>
      ))}
    </details>
  )
}
