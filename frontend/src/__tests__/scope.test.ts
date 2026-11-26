import { describe, expect, it } from 'vitest'
import { addLines, briefSummary, emptyScopeForm, placeEntry, scopeCheckKey, scopeLines, scopeOf, scopeProblemMark, scopeProblemText, scopeTone, withBrief } from '../scope'
import type { BriefResult } from '../types'

const brief = (over: Partial<BriefResult> = {}): BriefResult =>
  ({ name: 'example.com', in: [], out: [], unplaced: [], unsupported: [], notes: '', ...over })

describe('scope lists in the target form', () => {
  it('reads one entry a line and drops the blanks', () => {
    expect(scopeLines(' *.example.com \n\n api.example.com\n')).toEqual(['*.example.com', 'api.example.com'])
  })
  it('adds only what the list does not have yet, whatever the case', () => {
    expect(addLines('', ['a.example.com', 'b.example.com'])).toBe('a.example.com\nb.example.com')
    expect(addLines('A.example.com\n', ['a.example.com', 'c.example.com', 'c.example.com'])).toBe('A.example.com\nc.example.com')
    expect(addLines('typed by hand', [])).toBe('typed by hand')
  })
})

describe('a brief that was read', () => {
  it('fills an empty form', () => {
    const f = withBrief(emptyScopeForm(), brief({ in: ['*.example.com'], out: ['blog.example.com'], unplaced: ['www.example.org'], unsupported: ['*.example.*'], notes: 'Rules (from the brief):\n- No DoS' }))
    expect(f).toEqual({ name: 'example.com', notes: 'Rules (from the brief):\n- No DoS', inScope: '*.example.com', outScope: 'blog.example.com', unplaced: ['www.example.org'], unsupported: ['*.example.*'] })
  })
  it('adds to what is already there and keeps the name the owner gave', () => {
    const before = { ...emptyScopeForm(), name: 'Acme', notes: 'My account: hunter1', inScope: 'api.example.com', outScope: 'old.example.com', unplaced: ['x.example.net'] }
    const f = withBrief(before, brief({ in: ['api.example.com', 'app.example.com'], out: ['blog.example.com'], unplaced: ['old.example.com', 'y.example.net'], notes: 'Rules (from the brief):\n- No DoS' }))
    expect(f.name).toBe('Acme')
    expect(f.notes).toBe('My account: hunter1\n\nRules (from the brief):\n- No DoS')
    expect(f.inScope).toBe('api.example.com\napp.example.com')
    expect(f.outScope).toBe('old.example.com\nblog.example.com')
    expect(f.unplaced).toEqual(['x.example.net', 'y.example.net'])          // one the lists already hold needs no answer
  })
  it('does not repeat itself when the same brief is read twice', () => {
    const b = brief({ in: ['a.example.com'], unplaced: ['u.example.com'], unsupported: ['*.example.*'], notes: 'Rules (from the brief):\n- No DoS' })
    const once = withBrief(emptyScopeForm(), b)
    expect(withBrief(once, b)).toEqual(once)
  })
  it('puts an unplaced host on the side the owner picks', () => {
    const f = { ...emptyScopeForm(), inScope: 'a.example.com', unplaced: ['u.example.com', 'v.example.com'] }
    expect(placeEntry(f, 'u.example.com', 'in')).toMatchObject({ inScope: 'a.example.com\nu.example.com', unplaced: ['v.example.com'] })
    expect(placeEntry(f, 'v.example.com', 'out')).toMatchObject({ outScope: 'v.example.com', unplaced: ['u.example.com'] })
  })
  it('is saved as the two lists', () => {
    expect(scopeOf({ ...emptyScopeForm(), inScope: 'a.example.com\n\nb.example.com', outScope: ' c.example.com ', unplaced: ['never.saved'] }))
      .toEqual({ in: ['a.example.com', 'b.example.com'], out: ['c.example.com'] })
  })
  it('says in a line what it found', () => {
    expect(briefSummary(brief({ in: ['a', 'b', 'c'], out: ['d'], unplaced: ['e', 'f'] }))).toBe('Found 3 in scope, 1 out of scope and 2 with no side given. Check the lists before you save.')
    expect(briefSummary(brief({ in: ['a'] }))).toBe('Found 1 in scope. Check the lists before you save.')
    expect(briefSummary(brief({ unsupported: ['*.example.*'] }))).toBe('Found no host this text puts in or out of scope.')
    expect(briefSummary(brief())).toBe('Found no host this text puts in or out of scope.')
  })
})

describe('lines of a stored scope that cannot be read', () => {
  const problems = [
    { side: 'in' as const, line: 'acme.*', why: 'a * is read as the first label (*.example.com)' },
    { side: 'out' as const, line: '10.0.0.1-10.0.0.9', why: 'write a range of addresses as networks, like 10.0.0.0/26' },
  ]
  it('are listed for the owner, each with its side and what is wrong', () => {
    expect(scopeProblemText(problems)).toBe(
      'These scope lines cannot be read, so nothing is checked against them. Rewrite or remove them:\n'
      + 'In scope: acme.* (a * is read as the first label (*.example.com))\n'
      + 'Out of scope: 10.0.0.1-10.0.0.9 (write a range of addresses as networks, like 10.0.0.0/26)')
  })
  it('say nothing when every line is read, or when the server is an older one', () => {
    expect(scopeProblemText([])).toBe('')
    expect(scopeProblemText(undefined)).toBe('')
    expect(scopeProblemMark({ name: 'acme', scope_problems: [] })).toBe('')
    expect(scopeProblemMark({ name: 'acme' })).toBe('')
  })
  it('mark the target in the sidebar with how many there are', () => {
    expect(scopeProblemMark({ name: 'acme', scope_problems: problems })).toBe('2 scope lines of acme cannot be read and are not checked. Edit the target to fix them.')
    expect(scopeProblemMark({ name: 'acme', scope_problems: problems.slice(1) })).toBe('1 scope line of acme cannot be read and is not checked. Edit the target to fix it.')
  })
})

describe('checking one host against the lists of the form', () => {
  const f = { ...emptyScopeForm(), inScope: '*.example.com', outScope: 'blog.example.com' }
  it('ties an answer to the lists and the host it was given for, and to nothing else in the form', () => {
    const key = scopeCheckKey(f, 'shop.example.com')
    expect(scopeCheckKey({ ...f, name: 'Acme', notes: 'no DoS', unplaced: ['x.example.net'] }, ' shop.example.com ')).toBe(key)
    expect(scopeCheckKey({ ...f, inScope: ' *.example.com \n\n' }, 'shop.example.com')).toBe(key)      // the same lines, spaced differently
    expect(scopeCheckKey({ ...f, inScope: '*.example.com\napi.example.com' }, 'shop.example.com')).not.toBe(key)
    expect(scopeCheckKey({ ...f, outScope: '' }, 'shop.example.com')).not.toBe(key)
    expect(scopeCheckKey(f, 'api.example.com')).not.toBe(key)
  })
  it('tells a line on one list from the same line on the other', () => {
    expect(scopeCheckKey({ ...f, inScope: 'a.example.com\nb.example.com', outScope: '' }, 'x.example.com'))
      .not.toBe(scopeCheckKey({ ...f, inScope: 'a.example.com', outScope: 'b.example.com' }, 'x.example.com'))
    expect(scopeCheckKey({ ...f, inScope: 'a.example.com', outScope: '' }, 'b.example.com'))
      .not.toBe(scopeCheckKey({ ...f, inScope: '', outScope: 'a.example.com' }, 'b.example.com'))
  })
  it('marks an answer by what the check will do with a call', () => {
    expect(scopeTone('in')).toBe('ok')
    expect(scopeTone('out')).toBe('warn')
    expect(scopeTone('not_in')).toBe('warn')
    expect(scopeTone('no_list')).toBe('plain')
    expect(scopeTone('local')).toBe('plain')
  })
  it('marks the whole answer by the part that asks, where a command gets another answer', () => {
    expect(scopeTone('in', 'out')).toBe('warn')
    expect(scopeTone('out', 'in')).toBe('warn')
    expect(scopeTone('not_in', 'in')).toBe('warn')
    expect(scopeTone('local', 'out')).toBe('warn')
    expect(scopeTone('no_list', 'out')).toBe('warn')
    expect(scopeTone('local', 'in')).toBe('plain')
    expect(scopeTone('in', undefined)).toBe('ok')
  })
})
