import { describe, expect, it } from 'vitest'
import { FONT_NAME, OWN_FONT_FAMILY, OWN_FONT_FILE, SUGGESTED_FONTS, applyInterfaceFont, applyOwnFont, followFont, fontIsHere, rememberFont, rememberedFont } from '../font'

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

/** A page whose root element keeps what is set on it. */
function styled() {
  const props: Record<string, string> = {}
  return { props, doc: { documentElement: { dataset: {} as Record<string, string | undefined>,
    style: { setProperty: (k: string, v: string) => { props[k] = v }, removeProperty: (k: string) => { delete props[k] } } } } }
}

/** Widths as a browser measures them: text set in the first font of the list that it has. */
const measured = (has: Record<string, number>) => (family: string) => {
  for (const name of family.split(',').map((n) => n.trim().replace(/^'|'$/g, ''))) if (name in has) return has[name]
  return null
}
const BROWSER = { monospace: 600, serif: 520, 'sans-serif': 500 }

describe('the font the owner names for the interface', () => {
  it('goes in front of the fonts the interface has by itself', () => {
    const { doc, props } = styled()
    expect(applyInterfaceFont(doc, 'Ubuntu Mono', measured({ ...BROWSER, 'Ubuntu Mono': 500 }))).toBe(true)
    expect(props).toEqual({ '--sans': "'Ubuntu Mono', var(--sans-base)" })
    expect(doc.documentElement.dataset).toEqual({ chosen: 'yes' })
  })
  it('gives the page back to its own fonts when the name is taken away', () => {
    const { doc, props } = styled()
    applyInterfaceFont(doc, 'OCRA', measured({ ...BROWSER, OCRA: 715 }))
    expect(applyInterfaceFont(doc, '', measured(BROWSER))).toBe(false)
    expect(props).toEqual({})
    expect(doc.documentElement.dataset).toEqual({})
  })
  it('sets the text a little smaller for a font that is much wider than the usual one, and only for such a font', () => {
    const { doc } = styled()
    applyInterfaceFont(doc, 'OCRA', measured({ ...BROWSER, OCRA: 715 }))
    expect(doc.documentElement.dataset).toEqual({ chosen: 'yes', wide: 'yes' })
    applyInterfaceFont(doc, 'JetBrains Mono', measured({ ...BROWSER, 'JetBrains Mono': 590 }))          // 1.18 times: as it is
    expect(doc.documentElement.dataset).toEqual({ chosen: 'yes' })
    applyInterfaceFont(doc, 'Broad', measured({ ...BROWSER, Broad: 600 }))                              // 1.2 times: smaller
    expect(doc.documentElement.dataset).toEqual({ chosen: 'yes', wide: 'yes' })
  })
  it('is set although nothing can be measured, at the usual size', () => {
    const { doc, props } = styled()
    expect(applyInterfaceFont(doc, 'Ubuntu Mono', () => null)).toBe(true)
    expect(props['--sans']).toBe("'Ubuntu Mono', var(--sans-base)")
    expect(doc.documentElement.dataset).toEqual({ chosen: 'yes' })
  })
  it('takes a name and nothing that is more than a name', () => {
    for (const name of ["x', serif; } body { display: none", "'quoted'", "it's", 'say "so"', 'back\\slash', 'url(http://x)', '-leading', 'semi;colon', 'new\nline',
      'a'.repeat(61), ' ']) {
      const { doc, props } = styled()
      expect(applyInterfaceFont(doc, name, measured(BROWSER)), name).toBe(false)
      expect(props).toEqual({})
      expect(doc.documentElement.dataset).toEqual({})
      expect(FONT_NAME.test(name), name).toBe(false)
    }
    for (const name of ['Ubuntu Mono', 'M+ 1m', 'Noto Sans 日本語', 'DejaVu Sans Mono', '0xProto', 'a'.repeat(60)]) expect(FONT_NAME.test(name), name).toBe(true)
  })
  it('is offered with the fonts of Ubuntu first', () => {
    expect(SUGGESTED_FONTS.slice(0, 2)).toEqual(['Ubuntu Mono', 'Ubuntu Sans Mono'])
    expect(SUGGESTED_FONTS.every((name) => FONT_NAME.test(name))).toBe(true)
  })
})

describe('whether this browser has a font', () => {
  it('has it when text is set differently with it than without', () => {
    expect(fontIsHere('Ubuntu Mono', measured({ ...BROWSER, 'Ubuntu Mono': 500 }))).toBe(true)
    expect(fontIsHere('Mono Twin', measured({ ...BROWSER, 'Mono Twin': 600 }))).toBe(true)      // as wide as monospace, not as serif
  })
  it('does not have it when text comes out as in the fonts that follow it', () => {
    expect(fontIsHere('No Such Font', measured(BROWSER))).toBe(false)
  })
  it('cannot tell where nothing can be measured', () => {
    expect(fontIsHere('Ubuntu Mono', () => null)).toBeNull()
  })
})

describe('the name is kept in the browser, so that the page starts in the font', () => {
  const store = () => {
    const kept: Record<string, string> = {}
    return { kept, getItem: (k: string) => kept[k] ?? null, setItem: (k: string, v: string) => { kept[k] = v }, removeItem: (k: string) => { delete kept[k] } }
  }
  it('is kept, read again and taken away', () => {
    const s = store()
    expect(rememberedFont(s)).toBe('')
    rememberFont('Ubuntu Mono', s)
    expect(rememberedFont(s)).toBe('Ubuntu Mono')
    rememberFont('', s)
    expect(s.kept).toEqual({})
  })
  it('is no error where the browser keeps nothing', () => {
    const none = { getItem: () => { throw new Error('denied') }, setItem: () => { throw new Error('denied') }, removeItem: () => { throw new Error('denied') } }
    expect(rememberedFont(none)).toBe('')
    expect(() => rememberFont('Ubuntu Mono', none)).not.toThrow()
    expect(rememberedFont(undefined)).toBe('')
    expect(() => rememberFont('Ubuntu Mono', undefined)).not.toThrow()
  })
})

describe('the page follows the setting', () => {
  it('takes the font when the settings arrive and when the owner changes it, and keeps the name', () => {
    type S = { settings: { interface_font: string } | null }
    let listener: (now: S, was: S) => void = () => {}
    const { doc, props } = styled()
    const kept: Record<string, string> = {}
    const s = { getItem: (k: string) => kept[k] ?? null, setItem: (k: string, v: string) => { kept[k] = v }, removeItem: (k: string) => { delete kept[k] } }
    followFont((fn) => { listener = fn }, doc, s, measured({ ...BROWSER, 'Ubuntu Mono': 500 }))
    listener({ settings: null }, { settings: null })                                             // nothing is known yet
    expect(props).toEqual({})
    listener({ settings: { interface_font: 'Ubuntu Mono' } }, { settings: null })
    expect(props['--sans']).toBe("'Ubuntu Mono', var(--sans-base)")
    expect(kept).toEqual({ 'hunterai.interface-font': 'Ubuntu Mono' })
    delete props['--sans']
    listener({ settings: { interface_font: 'Ubuntu Mono' } }, { settings: { interface_font: 'Ubuntu Mono' } })   // another setting changed
    expect(props).toEqual({})
    listener({ settings: { interface_font: '' } }, { settings: { interface_font: 'Ubuntu Mono' } })
    expect(kept).toEqual({})
    expect(doc.documentElement.dataset).toEqual({})
  })
})

