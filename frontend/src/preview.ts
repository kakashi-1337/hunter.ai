/** The HTML preview of a response Caido recorded: what the body shows by itself.
 *
 * The body is somebody else's HTML, drawn inside this app. Three things keep it from doing anything:
 * - the frame it is drawn in has every sandbox restriction (ResponsePreview.tsx): no script runs, no form is sent,
 *   and it has no way to the app or out of its own box;
 * - the document begins with a content security policy that lets nothing be loaded from anywhere: only what is
 *   written into the page itself (inline style, `data:` pictures) is drawn. So the owner's browser sends no request
 *   to the target past Caido, and none to this app;
 * - what would go somewhere without a click, or with one, is taken out or pointed at nothing: a refresh, a base, link
 *   elements (a `dns-prefetch` is not covered by the policy), and where links lead.
 * The first is what the safety of the app rests on; the other two are about what the browser asks the network. */

/** Nothing from the network. The page's own inline style and pictures written into it are all that is drawn. */
export const PREVIEW_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; form-action 'none'; base-uri 'none'"

const XLINK = 'http://www.w3.org/1999/xlink'
const HTML_TYPES = ['text/html', 'application/xhtml+xml']
const CUT = /\n\[body cut at (\d+) characters of (\d+)\]$/                 // tools/caido_tools.py, _cut
const NOT_TEXT = /^\[(?:binary body|body compressed with|body said to be)[^\n]*\]$/     // what it writes in place of such a body

export interface HttpParts {
  head: string
  body: string
  /** The media type the response says its body is, in lower case; '' when it says none. */
  type: string
  /** Set when only the beginning of the body was kept. */
  cut: { shown: number; of: number } | null
}

/** A response as the Caido panel gets it (the head, an empty line, the body as text) taken apart. */
export function httpParts(text: string): HttpParts {
  const at = text.indexOf('\n\n')
  const head = at < 0 ? text : text.slice(0, at)
  let body = at < 0 ? '' : text.slice(at + 2)
  const said = head.split(/\r?\n/).slice(1).find((line) => /^content-type\s*:/i.test(line))
  const type = said ? said.slice(said.indexOf(':') + 1).split(';')[0].trim().toLowerCase() : ''
  const cut = CUT.exec(body)
  if (cut) body = body.slice(0, cut.index)
  return { head, body, type, cut: cut ? { shown: Number(cut[1]), of: Number(cut[2]) } : null }
}

/** Why there is nothing to render, in words for the owner; null when the body is HTML that can be drawn. */
export function previewProblem(parts: HttpParts): string | null {
  const body = parts.body.trim()
  if (!body) return 'This response has no body.'
  if (NOT_TEXT.test(body)) return `The body of this response is not available as text (${body}), so there is nothing to render.`
  if (HTML_TYPES.includes(parts.type)) return null
  if (parts.type) return `This response is ${parts.type}, not HTML: there is nothing to render. Its text is under Raw.`
  if (/^(?:﻿)?\s*<(?:!doctype\s+html|html[\s>])/i.test(parts.body)) return null
  return 'This response does not say it is HTML and does not begin like HTML: there is nothing to render. Its text is under Raw.'
}

/** The document the preview frame is given: the page's HTML with this preview's policy first in its head. */
export function previewDocument(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  // Scripts never run in the frame; they are taken out so that nothing of them is left to wonder about.
  doc.querySelectorAll('script, link, base, meta[http-equiv]').forEach((e) => e.remove())
  doc.querySelectorAll('a, area').forEach((e) => {
    const led = e.getAttribute('href') ?? e.getAttributeNS(XLINK, 'href')
    e.removeAttribute('href'); e.removeAttributeNS(XLINK, 'href'); e.removeAttribute('target'); e.removeAttribute('ping')
    if (led === null || e.namespaceURI !== 'http://www.w3.org/1999/xhtml') return
    e.setAttribute('href', '#')                                // it still looks like a link, and leads nowhere
    if (!e.hasAttribute('title')) e.setAttribute('title', led)
  })
  const policy = doc.createElement('meta')
  policy.setAttribute('http-equiv', 'Content-Security-Policy')
  policy.setAttribute('content', PREVIEW_POLICY)
  doc.head.prepend(policy)
  return `<!doctype html>${doc.documentElement.outerHTML}`
}
