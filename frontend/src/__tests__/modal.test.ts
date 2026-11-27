// @vitest-environment happy-dom
import { act, createElement, type ComponentProps, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DialogHost } from '../components/DialogHost'
import { Modal } from '../components/Modal'
import { confirmBox, promptBox, useDialog } from '../dialog'
import { BEHIND_DELAY_MS } from '../tree'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let root: Root
let host: HTMLElement

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })                // the clock a dialog reads, and nothing React waits on
  vi.setSystemTime(new Date('2026-11-27T10:00:00Z'))
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  useDialog.setState({ current: null, waiting: [] })
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.useRealTimers()
})
/** Time passes: by default the first moment of a dialog, in which a press behind it does not count. */
const later = (ms = BEHIND_DELAY_MS) => vi.setSystemTime(Date.now() + ms)

function show(node: ReactNode): void {
  act(() => root.render(node))
}
function fire(el: Element | Window, event: Event): void {
  act(() => { el.dispatchEvent(event) })
}
const press = (el: Element) => fire(el, new MouseEvent('mousedown', { bubbles: true }))
const one = <T extends Element>(selector: string) => host.querySelector<T>(selector)!

// createElement takes the children as arguments; the component's own type asks for them among the props.
const ModalAround = Modal as (props: Omit<ComponentProps<typeof Modal>, 'children'>) => ReactNode

/** A dialog that has been open for a moment, or one that `justOpened`. */
function modal(children: ReactNode, justOpened = false) {
  const onClose = vi.fn()
  show(createElement(ModalAround, { title: 'Edit target', onClose }, children))
  if (!justOpened) later()
  return onClose
}

describe('the first moment after a dialog appeared', () => {
  it('ignores a press on the page behind: the second press of a double click on the button that opened it lands there', () => {
    const onClose = modal(createElement('p', null, 'Three findings'), true)
    press(one('.overlay'))
    later(BEHIND_DELAY_MS - 1)
    press(one('.overlay'))
    expect(onClose).not.toHaveBeenCalled()
    later(1)
    press(one('.overlay'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('counts from when the dialog appeared, not from when it was last drawn', () => {
    const onClose = vi.fn()
    show(createElement(ModalAround, { title: 'Caido', onClose }, 'Asking Caido…'))
    later(BEHIND_DELAY_MS - 1)
    show(createElement(ModalAround, { title: 'Caido', onClose }, '50 requests'))      // its list came
    later(1)
    press(one('.overlay'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not hold back Escape', () => {
    const onClose = modal(createElement('p', null, 'Three findings'), true)
    fire(window, new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('a press on the page behind a dialog', () => {
  it('closes a dialog nothing was typed in', () => {
    const onClose = modal(createElement('input', { 'aria-label': 'Name' }))
    press(one('.overlay'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not close a dialog after something was typed in it', () => {
    const onClose = modal(createElement('textarea', { 'aria-label': 'Program brief or scope' }))
    fire(one('textarea'), new Event('input', { bubbles: true }))
    press(one('.overlay'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not close a dialog after a box in it was ticked or a choice was made', () => {
    const onClose = modal(createElement('select', { 'aria-label': 'Target' }, createElement('option', null, 'none')))
    fire(one('select'), new Event('change', { bubbles: true }))
    press(one('.overlay'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not close a dialog a file was dropped on', () => {
    const onClose = modal(createElement('div', { className: 'dropzone' }, 'Drop files here'))
    fire(one('.dropzone'), new Event('drop', { bubbles: true }))
    press(one('.overlay'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('is not a press inside the dialog, which never closes it', () => {
    const onClose = modal(createElement('p', null, 'Three findings'))
    press(one('.modal p'))
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('Escape', () => {
  it('closes a dialog even after something was typed in it', () => {
    const onClose = modal(createElement('input', { 'aria-label': 'Name' }))
    fire(one('input'), new Event('input', { bubbles: true }))
    fire(window, new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('a press behind a question', () => {
  it('answers no to a yes-or-no question', async () => {
    show(createElement(DialogHost))
    let asked!: Promise<boolean>
    act(() => { asked = confirmBox('Delete "notes" and all its messages?') })
    later()
    press(one('.overlay'))
    expect(await asked).toBe(false)
  })

  it('does not answer a question that has only just been asked: a double click on Delete would answer no to itself', async () => {
    show(createElement(DialogHost))
    let asked!: Promise<boolean>
    act(() => { asked = confirmBox('Delete "notes" and all its messages?') })
    press(one('.overlay'))
    expect(useDialog.getState().current?.message).toBe('Delete "notes" and all its messages?')
    later()
    press(one('.overlay'))
    expect(await asked).toBe(false)
  })

  it('cancels a question for a value while nothing was typed', async () => {
    show(createElement(DialogHost))
    let asked!: Promise<string | null>
    act(() => { asked = promptBox('New name', 'old name') })
    later()
    press(one('.overlay'))
    expect(await asked).toBeNull()
  })

  it('leaves a question for a value open once something was typed', async () => {
    show(createElement(DialogHost))
    let asked!: Promise<string | null>
    act(() => { asked = promptBox('New name', 'old name') })
    fire(one('input'), new Event('input', { bubbles: true }))
    later()
    press(one('.overlay'))
    expect(useDialog.getState().current?.message).toBe('New name')
    act(() => one<HTMLFormElement>('form').requestSubmit())
    expect(await asked).toBe('old name')
  })
})
