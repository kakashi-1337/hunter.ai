import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import { applyInterfaceFont, applyOwnFont, followFont, rememberedFont } from './font'
import { useStore } from './store'
import './theme.css'

applyOwnFont(document)
applyInterfaceFont(document, rememberedFont())      // the font the owner named, before the settings have arrived
followFont(useStore.subscribe, document)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
