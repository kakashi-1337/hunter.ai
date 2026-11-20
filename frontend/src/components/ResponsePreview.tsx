import { useMemo } from 'react'
import { httpParts, previewDocument, previewProblem } from '../preview'

/** The body of a recorded response drawn as a page, beside its raw text in the Caido panel.
 * `sandbox=""` is every restriction a frame can have: no script, no form, no pop-up, no way to this app or out of
 * the frame. It must stay empty: what is drawn is the target's HTML. See preview.ts for the rest. */
export function ResponsePreview({ text }: { text: string | null }) {
  const parts = useMemo(() => (text === null ? null : httpParts(text)), [text])
  const problem = parts === null ? 'No response was recorded for this request.' : previewProblem(parts)
  const page = useMemo(() => (parts !== null && problem === null ? previewDocument(parts.body) : ''), [parts, problem])
  if (parts === null || problem !== null) return <div className="hint caido-no-preview">{problem}</div>
  return (<>
    <iframe className="caido-preview" title="The response body drawn as a page" sandbox="" srcDoc={page} referrerPolicy="no-referrer" />
    <div className="hint">What the recorded HTML shows by itself: its scripts do not run and nothing is loaded from the network, so pictures and style sheets kept in other files are missing, and links lead nowhere (hover one to see where it pointed).
      {parts.cut && ` Only the first ${parts.cut.shown.toLocaleString('en-US')} of ${parts.cut.of.toLocaleString('en-US')} characters of the body were kept, so the page stops there.`}</div>
  </>)
}
