// @vitest-environment happy-dom
import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { MachineStatus } from '../components/MachineStatus'
import { localWindowHint, machineLine } from '../tree'
import type { LocalModel, SystemStatus } from '../types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const QUIET: SystemStatus = { cpu_temp: 55, cpu_limit: 90, local_models: [], restart_needed: false, stopped: [], page: null }
const GEMMA = { name: 'gemma3:12b', vram_mb: 6144, size_mb: 10240, processor: '40%/60% CPU/GPU', context: 32768 }
const QWEN = { name: 'qwen3:8b', vram_mb: 5120, size_mb: 5120, processor: '100% GPU', context: 16384 }

describe('the machine line', () => {
  it('says how much of a loaded model is on the GPU', () => {
    expect(machineLine({ ...QUIET, local_models: [QWEN] }).text).toBe('CPU 55°C · qwen3:8b loaded (5.0 GB, GPU)')
    expect(machineLine({ ...QUIET, local_models: [{ ...QWEN, vram_mb: 0, processor: '100% CPU' }] }).text).toBe('CPU 55°C · qwen3:8b loaded (5.0 GB, CPU)')
    expect(machineLine({ ...QUIET, local_models: [GEMMA] }).text).toBe('CPU 55°C · gemma3:12b loaded (10.0 GB, 60% GPU)')
  })

  it('turns yellow when a model is partly on the processor, and says what that means', () => {
    const { level, about } = machineLine({ ...QUIET, local_models: [GEMMA] })
    expect(level).toBe('warn')
    expect(about).toBe('gemma3:12b: 10.0 GB in memory, 40%/60% CPU/GPU as `ollama ps` says it, loaded with a window of 32,768 tokens. '
      + 'Part of it is on the processor, which makes it slow. On a machine with a graphics card it did not fit the memory of that card with this window: a smaller window or a smaller model fits.')
  })

  it('stays as it is for a model that is all on the GPU or all on the processor', () => {
    expect(machineLine({ ...QUIET, local_models: [QWEN] }).level).toBe('')
    expect(machineLine({ ...QUIET, local_models: [QWEN] }).about).toBe('qwen3:8b: 5.0 GB in memory, 100% GPU as `ollama ps` says it, loaded with a window of 16,384 tokens.')
    expect(machineLine({ ...QUIET, local_models: [{ ...QWEN, processor: '100% CPU', context: null }] }).level).toBe('')
  })

  it('keeps red for a processor at its limit, whatever the models do', () => {
    expect(machineLine({ ...QUIET, cpu_temp: 91, local_models: [GEMMA] }).level).toBe('bad')
  })

  it('names two loaded models each with its own share', () => {
    expect(machineLine({ ...QUIET, cpu_temp: null, local_models: [GEMMA, QWEN] }).text).toBe('gemma3:12b (10.0 GB, 60% GPU), qwen3:8b (5.0 GB, GPU) loaded')
  })

  it('reads the answer of a server from before it could tell where a model runs', () => {
    const old = { name: 'qwen3:8b', vram_mb: 5120 }
    const line = machineLine({ ...QUIET, local_models: [old] })
    expect(line.text).toBe('CPU 55°C · qwen3:8b loaded (5.0 GB)')
    expect(line.level).toBe('')
    expect(line.about).toBe('')
  })

  it('says what it said before when nothing is loaded or Ollama is not running', () => {
    expect(machineLine(QUIET).text).toBe('CPU 55°C · no local model loaded')
    expect(machineLine({ ...QUIET, local_models: null }).text).toBe('CPU 55°C · Ollama is not running')
    expect(machineLine({ ...QUIET, colibri: null }).text).toBe('CPU 55°C · no local model loaded')
  })

  it('names the model a colibri server holds, of which Ollama knows nothing', () => {
    const colibri = { model: 'qwen3.8-27b-colibri', context: 32768 }
    expect(machineLine({ ...QUIET, colibri }).text).toBe('CPU 55°C · qwen3.8-27b-colibri loaded (colibri)')
    expect(machineLine({ ...QUIET, local_models: null, colibri }).text).toBe('CPU 55°C · Ollama is not running · qwen3.8-27b-colibri loaded (colibri)')
    expect(machineLine({ ...QUIET, local_models: [QWEN], colibri }).text).toBe('CPU 55°C · qwen3:8b (5.0 GB, GPU), qwen3.8-27b-colibri (colibri) loaded')
    expect(machineLine({ ...QUIET, colibri }).about).toBe('qwen3.8-27b-colibri: held by colibri, with a context of 32,768 tokens.')
    expect(machineLine({ ...QUIET, colibri: { model: 'm', context: null } }).about).toBe('m: held by colibri.')
    expect(machineLine({ ...QUIET, colibri }).level).toBe('')
  })
})

describe('the machine line on the page', () => {
  let root: Root
  let host: HTMLElement
  beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
  afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks() })

  it('explains a model that is partly on the processor when it is pointed at', async () => {
    vi.spyOn(api, 'systemStatus').mockResolvedValue({ ...QUIET, local_models: [GEMMA] })
    await act(async () => { root.render(createElement(MachineStatus)) })
    const line = host.querySelector('.machine')!
    expect(line.className).toContain('warn')
    expect(line.getAttribute('title')).toContain('Part of it is on the processor, which makes it slow')
  })
})

const LOCAL: LocalModel = {
  answers: true, model: 'gemma3:12b', installed: true, window: 16384, limit: 131072, bytes_per_token: 393216,
  overhead: { tokens: 25608, tools: 112, on_demand: 40 }, enough: 36864, loaded: { processor: '100% GPU', context: 16384, size_mb: 9216 },
}

describe('what the window setting says about the local model in use', () => {
  it('says what a window costs for that model, how much the rules and tools take, and how it runs now', () => {
    expect(localWindowHint({ ...LOCAL, overhead: { tokens: 6752, tools: 36, on_demand: 0 }, enough: 16384 }, 16384)).toEqual({
      lines: [
        'With gemma3:12b, each 8,192 tokens of window take about 3.0 GB of memory beside the model itself. It takes 131,072 tokens at most.',
        "HunterAI's rules and tools take about 6,752 tokens of the window (36 tools).",
        'Ollama runs it 100% GPU with a window of 16,384 now.',
      ],
      warn: null,
    })
  })

  it('warns when the rules and tools leave no room for a chat, with the size to set', () => {
    const { lines, warn } = localWindowHint(LOCAL, 16384)
    expect(lines[1]).toBe("HunterAI's rules and tools take about 25,608 tokens of the window (112 tools, and 40 more that are loaded on demand).")
    expect(warn).toBe('Too small here: with the rules and tools, less than 8,192 tokens are left for the chat. Set 36,864 or more.')
  })

  it('does not warn once the window is large enough', () => {
    expect(localWindowHint(LOCAL, 36864).warn).toBeNull()
    expect(localWindowHint(LOCAL, 65536).warn).toBeNull()
  })

  it('says so when the model itself cannot take the window that would be enough', () => {
    expect(localWindowHint({ ...LOCAL, limit: 32768, window: 16384 }, 16384).warn).toBe(
      'Too small here: with the rules and tools, less than 8,192 tokens are left for the chat, and gemma3:12b takes 32,768 at most. '
      + 'Switch off the MCP servers you do not need, or choose a fallback model with a larger window.')
  })

  it('says what is not known when the model does not say what a window costs', () => {
    expect(localWindowHint({ ...LOCAL, bytes_per_token: null, limit: null, loaded: null }, 65536).lines[0])
      .toBe('What a larger window takes in memory depends on the model: `ollama ps` shows it once gemma3:12b is loaded.')
  })

  it('says that the fallback model is not installed, or that Ollama does not answer', () => {
    expect(localWindowHint({ ...LOCAL, installed: false, limit: null, bytes_per_token: null, loaded: null }, 65536).lines[0])
      .toBe('gemma3:12b, the fallback model, is not installed in Ollama (`ollama pull gemma3:12b`).')
    expect(localWindowHint({ ...LOCAL, answers: false, installed: false, limit: null, bytes_per_token: null, loaded: null }, 65536).lines[0])
      .toBe('Ollama does not answer, so gemma3:12b could not be asked what a window costs.')
  })

  it('says nothing more while the server has not answered', () => {
    expect(localWindowHint(null, 16384)).toEqual({ lines: [], warn: null })
  })
})
