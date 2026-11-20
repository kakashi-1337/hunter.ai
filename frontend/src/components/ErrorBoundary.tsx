import { Component, type ErrorInfo, type ReactNode } from 'react'

/** A part of the page that fails to draw shows what went wrong in its own place. Without this, one failing dialog
 * takes the whole interface with it and leaves an empty page. `resetKey`: what the part shows (a chat's id); when it
 * changes, the part is drawn again. `around`: what to put around the message (a dialog that can be closed). */
interface Props { children: ReactNode; resetKey?: string; around?: (message: ReactNode) => ReactNode }
interface State { error: Error | null; shown: string | undefined }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, shown: this.props.resetKey }

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey === state.shown ? null : { error: null, shown: props.resetKey }
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error('HunterAI: a part of the page could not be drawn.', error, info.componentStack)
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children
    const message = (
      <div className="draw-failed" role="alert">
        <b>This part of the page could not be shown.</b>
        <div className="mono">{error.message || String(error)}</div>
        <div>Reload the page (Ctrl+Shift+R). If HunterAI was updated a moment ago, restart it first: until then the server runs the old code and does not know what this page asks for.</div>
        <div className="row">
          <button className="btn" onClick={() => this.setState({ error: null })}>Try again</button>
          <button className="btn" onClick={() => window.location.reload()}>Reload the page</button>
        </div>
      </div>
    )
    return this.props.around ? this.props.around(message) : message
  }
}
