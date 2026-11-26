/** How the next token is chosen, as the servers for local models take it. A model names the ones its server takes
 * among its options (ModelInfo.options), and only those are shown for it. */
export const SAMPLING = ['top_k', 'min_p', 'repeat_penalty', 'repeat_last_n', 'presence_penalty', 'frequency_penalty', 'seed'] as const

/** The usual effort values. A model that takes fewer says which (ModelInfo.efforts). */
export const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'] as const

/** What the effort control of a model offers: the values the model takes, and the value the chat already has
 * when it is not one of them (the server sends that one as the model's nearest), so the control never looks unset. */
export function effortChoices(efforts: string[] | undefined, current: string | undefined): string[] {
  const offered = efforts?.length ? [...efforts] : [...EFFORTS]
  return current && !offered.includes(current) ? [...offered, current] : offered
}
