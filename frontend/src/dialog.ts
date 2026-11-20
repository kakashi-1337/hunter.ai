import { create } from 'zustand'

/** HunterAI's own "are you sure?" and "type a value" dialogs. The browser's confirm() and prompt() can be
 * switched off by the person at the keyboard ("don't allow this page to prompt you again"), after which
 * every delete silently does nothing. These cannot be switched off, and they look like the rest of the app. */
export interface DialogRequest {
  kind: 'confirm' | 'prompt'
  message: string
  initial: string
  danger: boolean
  resolve(value: boolean | string | null): void
}

interface DialogState { current: DialogRequest | null; waiting: DialogRequest[] }

export const useDialog = create<DialogState>(() => ({ current: null, waiting: [] }))

function open(request: DialogRequest): void {
  const s = useDialog.getState()
  if (s.current) useDialog.setState({ waiting: [...s.waiting, request] })
  else useDialog.setState({ current: request })
}

/** Answer the dialog on screen and show the next one that waits, if any. */
export function answer(value: boolean | string | null): void {
  const { current, waiting } = useDialog.getState()
  if (!current) return
  useDialog.setState({ current: waiting[0] ?? null, waiting: waiting.slice(1) })
  current.resolve(value)
}

/** Resolves true when the owner agrees. A question that starts with Delete, Remove, Clear or Forget gets a red button,
 * and so does one the caller marks as dangerous. */
export function confirmBox(message: string, danger = false): Promise<boolean> {
  return new Promise((resolve) => open({
    kind: 'confirm', message, initial: '', danger: danger || /^(delete|remove|clear|forget)\b/i.test(message.trim()),
    resolve: (v) => resolve(v === true),
  }))
}

/** Resolves the typed text, or null when the owner cancels. */
export function promptBox(message: string, initial = ''): Promise<string | null> {
  return new Promise((resolve) => open({
    kind: 'prompt', message, initial, danger: false, resolve: (v) => resolve(typeof v === 'string' ? v : null),
  }))
}
