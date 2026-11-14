import { isValidElement, memo, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'

function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children)
  return ''
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const child = Array.isArray(children) ? children[0] : children
  const className = isValidElement<{ className?: string }>(child) ? (child.props.className ?? '') : ''
  const lang = /language-([\w+#.-]+)/.exec(className)?.[1] ?? 'text'
  const copy = async () => {
    setState((await copyText(textOf(children).replace(/\n$/, ''))) ? 'copied' : 'failed')
    setTimeout(() => setState('idle'), 1500)
  }
  return (
    <div className="codeblock">
      <div className="bar">
        <span className="mono">{lang}</span>
        <button className="icon" onClick={copy}>{state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}</button>
      </div>
      <pre>{children}</pre>
    </div>
  )
}

/**
 * Model output rendered as Markdown. Raw HTML in the text is NOT rendered
 * (react-markdown escapes it unless a raw-HTML plugin is added, and none is).
 */
export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
          img: ({ alt, src }) => <span className="muted">[image: {alt || (typeof src === 'string' ? src : '')}]</span>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
})
