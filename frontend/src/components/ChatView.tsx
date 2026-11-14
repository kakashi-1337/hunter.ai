import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
import { useStore } from '../store'
import { ChatKbBar } from './ChatKbBar'
import { Composer } from './Composer'
import { GoalStrip } from './GoalStrip'
import { MessageItem } from './MessageItem'
import { PermissionPrompt } from './PermissionPrompt'

export function ChatView({ header }: { header?: ReactNode }) {
  const chat = useStore((s) => s.chat)
  const messages = useStore((s) => s.messages)
  const asks = useStore((s) => s.asks)
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  // Follow new output only while the reader is already at the bottom.
  const onScroll = () => {
    const el = scroller.current
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }
  useLayoutEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [messages, asks])
  useEffect(() => { pinned.current = true }, [chat?.id])

  return (
    <main className="main">
      {header}
      <GoalStrip />
      <div className="messages" ref={scroller} onScroll={onScroll}>
        {messages.length === 0 ? (
          <div className="welcome">
            <h1>Hunter<span style={{ color: 'var(--accent)' }}>AI</span></h1>
            <p>{chat ? 'Start this chat with a message below.' : 'Type a message to start a new chat, or pick one on the left.'}</p>
          </div>
        ) : messages.map((m) => <MessageItem key={m.id} message={m} />)}
      </div>
      {asks[0] && <PermissionPrompt key={asks[0].id} request={asks[0]} waiting={asks.length} />}
      <ChatKbBar />
      <Composer />
    </main>
  )
}
