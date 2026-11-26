/** The owner's own interface font.
 *
 * A font the owner has a licence for is his to use and not this repository's to hand out, so its file is not part
 * of the code: he puts it at `frontend/public/fonts/OCRAStd.woff2` (git leaves that folder alone), and the next
 * build finds it. Whether it is there is decided when the interface is built (`__OWN_FONT__`, vite.config.ts), so
 * a page built without the file asks for no font and looks as it always did. */

declare const __OWN_FONT__: boolean

export const OWN_FONT_FILE = 'fonts/OCRAStd.woff2'      // under frontend/public, which a build copies as it is
export const OWN_FONT_FAMILY = 'OCR A Std'

interface Page {
  documentElement: { dataset: Record<string, string | undefined> }
  fonts: { add(face: never): unknown }
}
type FaceMaker = new (family: string, source: string, descriptors?: { display?: 'swap' }) => unknown

/** Put the owner's font in front of the interface's own (theme.css reads `data-font`). True when it was done. */
export function applyOwnFont(doc: Page, found: boolean = __OWN_FONT__,
  Face: FaceMaker | undefined = (globalThis as { FontFace?: FaceMaker }).FontFace): boolean {
  if (!found || !Face) return false
  try {
    doc.fonts.add(new Face(OWN_FONT_FAMILY, `url('/${OWN_FONT_FILE}') format('woff2')`, { display: 'swap' }) as never)
  } catch {
    return false        // the page keeps the font it has
  }
  doc.documentElement.dataset.font = 'own'
  return true
}

/* ---- A font the owner names

 * The interface is set in the fonts of the system. In Settings the owner can name a font that is installed on his
 * computer instead ("Ubuntu Mono"): no file, no build. The browser takes a font of the computer by its name, so the
 * name goes in front of the interface's own fonts (theme.css: --sans-base), and what the font lacks falls to them. */

/** The name of a font, and nothing that could be more than a name where it is put into a style. The server asks the same. */
export const FONT_NAME = /^[\p{L}\p{N}][\p{L}\p{N}_ .+-]{0,59}$/u
/** Offered in Settings; any installed font can be typed. "Ubuntu Sans Mono" is what newer Ubuntu calls its terminal font. */
export const SUGGESTED_FONTS = ['Ubuntu Mono', 'Ubuntu Sans Mono', 'Ubuntu Sans', 'Ubuntu', 'JetBrains Mono', 'Fira Code', 'DejaVu Sans Mono',
  'Noto Sans', 'OCRA']
/** A font this many times as wide as the usual one is set a little smaller, as the owner's own OCR-A file is. */
export const WIDE = 1.2
const KEPT = 'hunterai.interface-font'
const SAMPLE = 'The quick brown fox jumps over the lazy dog 0123456789'

/** How wide a sample comes out in a font list; null where that cannot be measured. */
export type Measure = (family: string) => number | null

interface Styled {
  documentElement: { dataset: Record<string, string | undefined>; style: { setProperty(name: string, value: string): void; removeProperty(name: string): unknown } }
}
interface Kept { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void }

/** The browser's own measure, by a canvas. */
export const widthIn: Measure = (family) => {
  try {
    const ctx = document.createElement('canvas').getContext('2d')
    if (!ctx) return null
    ctx.font = `100px ${family}`
    return ctx.measureText(SAMPLE).width
  } catch {
    return null
  }
}

/** Whether this browser has a font of that name: text set in it comes out differently than in what follows it.
 * null where nothing can be measured. */
export function fontIsHere(name: string, measure: Measure = widthIn): boolean | null {
  const pairs = ['monospace', 'serif'].map((generic) => [measure(`'${name}', ${generic}`), measure(generic)] as const)
  if (pairs.some(([own, plain]) => own === null || plain === null)) return null
  return pairs.some(([own, plain]) => own !== plain)
}

/** Set the interface in the font of that name, or in its own fonts again for an empty name. True when a name is in use. */
export function applyInterfaceFont(doc: Styled, name: string, measure: Measure = widthIn): boolean {
  const root = doc.documentElement
  delete root.dataset.wide
  if (!FONT_NAME.test(name)) {
    root.style.removeProperty('--sans')
    delete root.dataset.chosen
    return false
  }
  root.style.setProperty('--sans', `'${name}', var(--sans-base)`)
  root.dataset.chosen = 'yes'
  const own = measure(`'${name}', sans-serif`)
  const usual = measure('sans-serif')
  if (own !== null && usual !== null && usual > 0 && own / usual >= WIDE) root.dataset.wide = 'yes'
  return true
}

const browserStore = (): Kept | undefined => (globalThis as { localStorage?: Kept }).localStorage

/** Keep the name in the browser: the page starts in the font, before the settings have arrived. */
export function rememberFont(name: string, store: Kept | undefined = browserStore()): void {
  try {
    if (name) store?.setItem(KEPT, name)
    else store?.removeItem(KEPT)
  } catch { /* a browser that keeps nothing: the font is there once the settings are */ }
}

export function rememberedFont(store: Kept | undefined = browserStore()): string {
  try {
    return store?.getItem(KEPT) ?? ''
  } catch {
    return ''
  }
}

interface WithFont { settings: { interface_font: string } | null }

/** Follow the setting: when the settings arrive, and whenever the owner names another font. */
export function followFont(subscribe: (listener: (now: WithFont, was: WithFont) => void) => unknown, doc: Styled,
  store: Kept | undefined = browserStore(), measure: Measure = widthIn): void {
  subscribe((now, was) => {
    const name = now.settings?.interface_font
    if (name === undefined || name === was.settings?.interface_font) return
    applyInterfaceFont(doc, name, measure)
    rememberFont(name, store)
  })
}

