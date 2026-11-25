import { describe, expect, it } from 'vitest'
import { OWN_FONT_FAMILY, OWN_FONT_FILE, applyOwnFont } from '../font'

/** What a page gives the function: the root element's data set, and its set of fonts. */
function page() {
  const added: unknown[] = []
  return { added, doc: { documentElement: { dataset: {} as Record<string, string | undefined> }, fonts: { add: (f: unknown) => { added.push(f) } } } }
}

class Face {
  family: string; source: string; descriptors: unknown
  constructor(family: string, source: string, descriptors?: unknown) {
    this.family = family; this.source = source; this.descriptors = descriptors
  }
}

describe("the owner's own interface font", () => {
  it('is the file the owner named, under the folder a build copies as it is', () => {
    expect(OWN_FONT_FILE).toBe('fonts/OCRAStd.woff2')
    expect(OWN_FONT_FAMILY).toBe('OCR A Std')
  })
  it('is taken into use when the build found the file', () => {
    const { doc, added } = page()
    expect(applyOwnFont(doc, true, Face)).toBe(true)
    expect(doc.documentElement.dataset.font).toBe('own')
    expect(added).toEqual([new Face('OCR A Std', "url('/fonts/OCRAStd.woff2') format('woff2')", { display: 'swap' })])
  })
  it('changes nothing when the build found no file: the page looks as it always did, and asks for no font', () => {
    const { doc, added } = page()
    expect(applyOwnFont(doc, false, Face)).toBe(false)
    expect(doc.documentElement.dataset).toEqual({})
    expect(added).toEqual([])
  })
  it('leaves the page as it is in a browser that cannot take a font from a script', () => {
    const { doc, added } = page()
    expect(applyOwnFont(doc, true, undefined)).toBe(false)
    expect(doc.documentElement.dataset).toEqual({})
    expect(added).toEqual([])
    const throws = { documentElement: { dataset: {} as Record<string, string | undefined> }, fonts: { add: () => { throw new Error('no') } } }
    expect(applyOwnFont(throws, true, Face)).toBe(false)
    expect(throws.documentElement.dataset).toEqual({})
  })
})
