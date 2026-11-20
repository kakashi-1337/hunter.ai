// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ResponsePreview } from '../components/ResponsePreview'
import { PREVIEW_POLICY, httpParts, previewDocument, previewProblem } from '../preview'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const HEAD = 'HTTP/1.1 200 OK\r\nServer: nginx\r\nContent-Type: text/html; charset=utf-8'
const PAGE = '<!doctype html><html><head><title>Shop</title><style>h1 { color: red }</style></head><body><h1>Welcome</h1></body></html>'
const response = (body: string, head = HEAD) => `${head}\n\n${body}`

describe('a recorded response taken apart', () => {
  it('gives the head, the body and what the body says it is', () => {
    const parts = httpParts(response(PAGE))
    expect(parts.head).toBe(HEAD)
    expect(parts.body).toBe(PAGE)
    expect(parts.type).toBe('text/html')
    expect(parts.cut).toBeNull()
  })
  it('takes the note of a cut body off the body and keeps what it says', () => {
    const parts = httpParts(response(`${PAGE}\n[body cut at 20000 characters of 123456]`))
    expect(parts.body).toBe(PAGE)
    expect(parts.cut).toEqual({ shown: 20000, of: 123456 })
  })
  it('knows a response without a body', () => {
    expect(httpParts('HTTP/1.1 204 No Content\r\nServer: nginx').body).toBe('')
  })
})

describe('whether there is something to render', () => {
  it('renders HTML, by its content type or, without one, by how it begins', () => {
    expect(previewProblem(httpParts(response(PAGE)))).toBeNull()
    expect(previewProblem(httpParts(response(PAGE, 'HTTP/1.1 200 OK\r\nContent-Type: application/xhtml+xml')))).toBeNull()
    expect(previewProblem(httpParts(response('  <!DOCTYPE HTML><p>x', 'HTTP/1.1 200 OK')))).toBeNull()
    expect(previewProblem(httpParts(response('<html><p>x', 'HTTP/1.1 200 OK')))).toBeNull()
  })
  it('says what a response is when it is not HTML', () => {
    expect(previewProblem(httpParts(response('{"a": 1}', 'HTTP/1.1 200 OK\r\nContent-Type: application/json'))))
      .toBe('This response is application/json, not HTML: there is nothing to render. Its text is under Raw.')
    expect(previewProblem(httpParts(response('just words', 'HTTP/1.1 200 OK'))))
      .toBe('This response does not say it is HTML and does not begin like HTML: there is nothing to render. Its text is under Raw.')
  })
  it('says why when the body is not there as text', () => {
    expect(previewProblem(httpParts(response('[body compressed with br, 5120 bytes: not shown]'))))
      .toBe('The body of this response is not available as text ([body compressed with br, 5120 bytes: not shown]), so there is nothing to render.')
    expect(previewProblem(httpParts(response('[binary body, 900 bytes, text/html]')))).toContain('not available as text')
    expect(previewProblem(httpParts('HTTP/1.1 304 Not Modified\r\nContent-Type: text/html'))).toBe('This response has no body.')
  })
})

describe('the document the preview frame is given', () => {
  const HOSTILE = `<!doctype html><html><head>
    <meta http-equiv="refresh" content="0; url=https://evil.example/">
    <meta http-equiv="Content-Security-Policy" content="style-src 'none'">
    <base href="https://evil.example/">
    <link rel="stylesheet" href="https://cdn.example/app.css"><link rel="dns-prefetch" href="//track.example">
    <script src="https://cdn.example/app.js"></script><script>document.title = 'ran'</script>
    <style>h1 { color: red }</style></head>
    <body><h1 style="margin: 0">Welcome</h1><a href="https://shop.example/account" target="_top">Account</a>
    <img src="https://track.example/pixel.gif"><form action="/login"><input name="user"><button>Go</button></form>
    <svg><a href="https://evil.example/svg"><text>x</text></a></svg></body></html>`

  it('begins with its own policy, before anything of the page', () => {
    const doc = previewDocument(PAGE)
    expect(doc.startsWith('<!doctype html><html><head><meta http-equiv="Content-Security-Policy"')).toBe(true)
    expect(doc).toContain(`content="${PREVIEW_POLICY}"`)
    expect(doc).toContain('<h1>Welcome</h1>')
    expect(doc).toContain('h1 { color: red }')                  // the page's own inline style stays
  })
  it('lets nothing be loaded but what is inside the page itself', () => {
    expect(PREVIEW_POLICY).toBe("default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; form-action 'none'; base-uri 'none'")
  })
  it('takes out what would go somewhere by itself, and what the page set against its own look', () => {
    const doc = previewDocument(HOSTILE)
    expect(doc).not.toContain('refresh')
    expect(doc).not.toContain("style-src 'none'")
    expect(doc).not.toContain('<base')
    expect(doc).not.toContain('<link')
    expect(doc).not.toContain('<script')
    expect(doc.match(/http-equiv/g)).toHaveLength(1)            // the one this preview adds
    expect(doc).toContain('<h1 style="margin: 0">Welcome</h1>')
    expect(doc).toContain('<input name="user">')                // a form is shown; the frame cannot send it
  })
  it('makes a link show where it led without leading there', () => {
    const doc = previewDocument(HOSTILE)
    expect(doc).toContain('<a href="#" title="https://shop.example/account">Account</a>')
    expect(doc).not.toContain('https://evil.example')
    expect(doc).not.toContain('target=')
  })
})

describe('the preview as the panel draws it', () => {
  let host: HTMLDivElement
  let root: Root
  beforeEach(() => { host = document.createElement('div'); document.body.append(host); root = createRoot(host) })
  afterEach(() => { act(() => root.unmount()); host.remove() })
  const draw = (text: string | null) => act(() => { root.render(createElement(ResponsePreview, { text })) })

  it('is a frame that can run nothing and reach nothing of the app', () => {
    draw(response(PAGE))
    const frame = host.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe('')              // every restriction: no script, no form, no way out of the frame
    expect(frame.getAttribute('srcdoc')).toBe(previewDocument(PAGE))
    expect(frame.hasAttribute('src')).toBe(false)
    expect(host.textContent).toContain('scripts do not run and nothing is loaded from the network')
  })
  it('says how much of a cut body it shows', () => {
    draw(response(`${PAGE}\n[body cut at 20000 characters of 123456]`))
    expect(host.textContent).toContain('Only the first 20,000 of 123,456 characters of the body were kept')
  })
  it('says why instead of drawing an empty frame', () => {
    draw(response('{"a": 1}', 'HTTP/1.1 200 OK\r\nContent-Type: application/json'))
    expect(host.querySelector('iframe')).toBeNull()
    expect(host.textContent).toContain('This response is application/json, not HTML')
    draw(null)
    expect(host.textContent).toContain('No response was recorded for this request.')
  })
})
