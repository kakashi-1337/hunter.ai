import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The owner's own interface font, when he put one there (src/font.ts). The file is his and not in the repository.
const ownFont = existsSync(fileURLToPath(new URL('./public/fonts/OCRAStd.woff2', import.meta.url)))

export default defineConfig({
  plugins: [react()],
  define: { __OWN_FONT__: JSON.stringify(ownFont) },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: `http://127.0.0.1:${process.env.HUNTERAI_PORT || 8790}`, changeOrigin: false } },
  },
  test: { environment: 'node', include: ['src/__tests__/**/*.test.ts'] },
})
