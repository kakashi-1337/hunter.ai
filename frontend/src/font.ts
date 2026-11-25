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
