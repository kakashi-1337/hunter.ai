import { useEffect, useState } from 'react'

/** How long a value has to stay as it is before it counts as settled. */
export const SETTLE_MS = 200

/** `value`, once it has stopped changing for `ms`: a burst of changes is seen as one. What asks the server anew
 * with every step of a run asks once for the steps a replay brings within a moment. */
export function useSettled<T>(value: T, ms: number = SETTLE_MS): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return settled
}
