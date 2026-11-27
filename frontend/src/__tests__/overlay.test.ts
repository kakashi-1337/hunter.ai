import { describe, expect, it } from 'vitest'
import css from '../theme.css?raw'

/** What the style sheet declares for exactly this selector. */
const declared = (selector: string): string => {
  const rule = css.split('\n').find((line) => line.startsWith(`${selector} {`))
  return rule ? rule.slice(rule.indexOf('{') + 1, rule.lastIndexOf('}')) : ''
}

describe('the page behind a dialog', () => {
  // No layout is computed in these tests, so this holds the declaration itself. What it is for was measured in a
  // browser: the one column of that page was as wide as the longest line in the dialog that cannot be broken, an
  // address in Caido's list for one. The dialog stands in the middle of its column, so a list with an address of 200
  // characters moved it to the side of the screen, and one of 800 characters moved it past the right edge: only the
  // dark page was left to see, and a press on it closed a dialog its owner had never seen.
  it('gives a dialog a place as wide as the screen and no wider, whatever stands in the dialog', () => {
    expect(declared('.overlay')).toMatch(/(^|;)\s*grid-template-columns:\s*minmax\(0, 1fr\)\s*;/)
    expect(declared('.overlay')).toMatch(/(^|;)\s*place-items:\s*center\s*;/)
    expect(declared('.overlay')).toMatch(/(^|;)\s*position:\s*fixed\s*;/)
  })
})
