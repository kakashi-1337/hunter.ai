import type {
  ActivitySummary, QueuedMessage, CaidoExchange, CaidoRow, CaidoStatus, Checkup, FindingRow, SystemStatus,
  Memory, MemorySettings,
  Persona,
  McpCheck, McpInspection, McpReview, McpScan, McpServer,
  KbChunk,
  ChatKb, Collection, Job, JobAction, KbDocument, KbHealth, KbHit, KbStatus, UploadResult,
  Chat, Finding, Goal, Grant, McpStatus, Message, Mode, Options, PermissionDecision, PermissionRequest, ProcessInfo, ProviderInfo,
  RunDetail, RunSummary, SearchHit, Settings, StartedRun, Target, ToolInfo, Upload,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined || body instanceof FormData ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'Cannot reach the HunterAI backend. Is it running?')
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (typeof j?.detail === 'string') message = j.detail
      else if (Array.isArray(j?.detail) && j.detail[0]?.msg) message = j.detail[0].msg
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message)
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
}

const q = (params: Record<string, string | null | undefined>) => {
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v) s.set(k, v)
  const out = s.toString()
  return out ? `?${out}` : ''
}

export const listTargets = () => request<Target[]>('GET', '/api/targets')
export const createTarget = (name: string) => request<Target>('POST', '/api/targets', { name })
export const updateTarget = (id: string, patch: Partial<Pick<Target, 'name' | 'notes' | 'scope'>>) =>
  request<Target>('PATCH', `/api/targets/${id}`, patch)
export const deleteTarget = (id: string) => request<void>('DELETE', `/api/targets/${id}`)

export const listChats = (targetId?: string) => request<Chat[]>('GET', `/api/chats${q({ target_id: targetId })}`)
export const createChat = (body: { target_id?: string | null; mode?: Mode; provider?: string; model?: string; options?: Options; persona_id?: string | null }) =>
  request<Chat>('POST', '/api/chats', body)
export const getChat = (id: string) => request<{ chat: Chat; messages: Message[]; workdir?: string }>('GET', `/api/chats/${id}`)
export const updateChat = (
  id: string, patch: Partial<Pick<Chat, 'title' | 'mode' | 'provider' | 'model' | 'options' | 'target_id' | 'workdir' | 'permissions' | 'persona_id'>>,
) => request<Chat>('PATCH', `/api/chats/${id}`, patch)
export const deleteChat = (id: string) => request<void>('DELETE', `/api/chats/${id}`)

export const sendMessage = (chatId: string, text: string, uploadIds: string[]) =>
  request<StartedRun>('POST', `/api/chats/${chatId}/messages`, { text, upload_ids: uploadIds })
export const editMessage = (id: string, text: string) => request<StartedRun>('POST', `/api/messages/${id}/edit`, { text })
export const retryMessage = (id: string) => request<StartedRun>('POST', `/api/messages/${id}/retry`)
export const deleteMessage = (id: string) => request<void>('DELETE', `/api/messages/${id}`)
export const setActiveLeaf = (chatId: string, messageId: string) =>
  request<{ ok: boolean }>('POST', `/api/chats/${chatId}/active-leaf`, { message_id: messageId })

export const cancelRun = (id: string) => request<{ cancelled: boolean }>('POST', `/api/runs/${id}/cancel`)
export const getRun = (id: string) => request<RunDetail>('GET', `/api/runs/${id}`)
export const listRuns = (chatId: string) => request<RunSummary[]>('GET', `/api/chats/${chatId}/runs`)

export const upload = (file: File, chatId?: string) => {
  const form = new FormData()
  form.append('file', file, file.name || 'pasted')
  if (chatId) form.append('chat_id', chatId)
  return request<Upload>('POST', '/api/uploads', form)
}
export const uploadUrl = (id: string) => `/api/uploads/${id}`

export const search = (text: string) => request<SearchHit[]>('GET', `/api/search${q({ q: text })}`)
export const listProviders = () => request<ProviderInfo[]>('GET', '/api/providers')
export const getSettings = () => request<Settings>('GET', '/api/settings')
export const putSettings = (patch: Partial<Pick<Settings, 'default_provider' | 'default_model' | 'fallback_model' | 'permissions' | 'max_turns' | 'decision_provider' | 'decision_model' | 'decision_gate' | 'kb_redact' | 'vision_model' | 'browser_via_caido' | 'answer_check' | 'mcp_on_demand' | 'notify_findings' | 'notify_min_severity' | 'decision_engine'>>) =>
  request<Settings>('PUT', '/api/settings', patch)
export const putKey = (name: string, value: string) => request<void>('PUT', `/api/settings/keys/${name}`, { value })

export const pendingPermissions = (runId: string) => request<PermissionRequest[]>('GET', `/api/runs/${runId}/permissions`)
export const answerPermission = (runId: string, requestId: string, decision: PermissionDecision, note: string) =>
  request<{ ok: boolean }>('POST', `/api/runs/${runId}/permissions/${requestId}`, { decision, note })
export const listTools = (chatId?: string) => request<ToolInfo[]>('GET', `/api/tools${q({ chat_id: chatId })}`)
export const listGrants = (chatId: string) => request<Grant[]>('GET', `/api/chats/${chatId}/grants`)
export const clearGrants = (chatId: string) => request<void>('DELETE', `/api/chats/${chatId}/grants`)
export const listProcesses = (chatId: string) => request<ProcessInfo[]>('GET', `/api/chats/${chatId}/processes`)
export const processOutput = (id: string, offset: number) =>
  request<{ text: string; next_offset: number; total: number; status: ProcessInfo['status']; exit_code: number | null }>(
    'GET', `/api/processes/${id}/output?offset=${offset}`)
export const processInput = (id: string, text: string, eof = false) =>
  request<{ ok: boolean }>('POST', `/api/processes/${id}/input`, { text, eof })
export const processKill = (id: string) => request<ProcessInfo>('POST', `/api/processes/${id}/kill`)
export const mcpStatus = () => request<McpStatus>('GET', '/api/mcp')
export const mcpSwitch = (name: string, enabled: boolean) => request<McpStatus>('PUT', `/api/mcp/${name}`, { enabled })
export const getGoal = (chatId: string) => request<Goal | null>('GET', `/api/chats/${chatId}/goal`)
export const clearGoal = (chatId: string) => request<void>('DELETE', `/api/chats/${chatId}/goal`)
export const allFindings = () => request<FindingRow[]>('GET', '/api/findings')
export const listFindings = (targetId: string) => request<Finding[]>('GET', `/api/targets/${targetId}/findings`)
export const patchFinding = (id: string, patch: Partial<Pick<Finding, 'title' | 'severity' | 'status' | 'evidence'>>) =>
  request<Finding>('PATCH', `/api/findings/${id}`, patch)
export const deleteFinding = (id: string) => request<void>('DELETE', `/api/findings/${id}`)

// ---- knowledge base
export const kbHealth = () => request<KbHealth>('GET', '/api/kb/health')
export const listCollections = () => request<Collection[]>('GET', '/api/kb/collections')
export const createCollection = (name: string, embedder?: string) =>
  request<Collection>('POST', '/api/kb/collections', embedder ? { name, embedder } : { name })
export const renameCollection = (id: string, name: string) => request<Collection>('PATCH', `/api/kb/collections/${id}`, { name })
export const deleteCollection = (id: string) => request<void>('DELETE', `/api/kb/collections/${id}`)
export const kbStatus = (id: string) => request<KbStatus>('GET', `/api/kb/collections/${id}/status`)
export const listDocuments = (id: string, opts: { status?: string; limit?: number; offset?: number } = {}) =>
  request<{ total: number; items: KbDocument[] }>('GET', `/api/kb/collections/${id}/documents${q({
    status: opts.status, limit: opts.limit ? String(opts.limit) : undefined, offset: opts.offset ? String(opts.offset) : undefined })}`)
export const startUpload = (collectionId: string) => request<{ upload_id: string }>('POST', `/api/kb/collections/${collectionId}/uploads`)
export const uploadFiles = (uploadId: string, items: { file: File; path: string }[], tooLarge: string[] = []) => {
  const form = new FormData()
  for (const it of items) { form.append('files', it.file, it.file.name || 'file'); form.append('paths', it.path) }
  for (const p of tooLarge) form.append('too_large', p)
  return request<UploadResult>('POST', `/api/kb/uploads/${uploadId}/files`, form)
}
export const commitUpload = (uploadId: string) => request<Job>('POST', `/api/kb/uploads/${uploadId}/commit`)
export const abandonUpload = (uploadId: string) => request<void>('DELETE', `/api/kb/uploads/${uploadId}`)
export const ingestFolder = (collectionId: string, path: string, again = false) =>
  request<Job>('POST', `/api/kb/collections/${collectionId}/ingest/folder`, { path, again })
export const deleteJob = (id: string) => request<void>('DELETE', `/api/kb/jobs/${id}`)
export const pruneJobs = (collectionId: string) => request<{ removed: number }>('POST', `/api/kb/collections/${collectionId}/jobs/prune`)
export const listJobs = (collectionId?: string) => request<Job[]>('GET', `/api/kb/jobs${q({ collection_id: collectionId })}`)
export const jobAction = (id: string, action: JobAction) => request<Job>('POST', `/api/kb/jobs/${id}/${action}`)
export const kbSearch = (collectionIds: string[], query: string, limit = 8) =>
  request<{ hits: KbHit[]; duration_ms: number }>('POST', '/api/kb/search', { collection_ids: collectionIds, query, limit })
export const getChatKb = (chatId: string) => request<ChatKb>('GET', `/api/kb/chats/${chatId}`)
export const putChatKb = (chatId: string, patch: Partial<Pick<ChatKb, 'collection_ids' | 'always_retrieve'>>) =>
  request<ChatKb>('PUT', `/api/kb/chats/${chatId}`, patch)
export const ingestGit = (collectionId: string, url: string, again = false) =>
  request<Job>('POST', `/api/kb/collections/${collectionId}/ingest/git`, { url, again })
export const ingestWeb = (collectionId: string, body: { url: string; max_pages: number; max_depth: number; robots: boolean; allow_local: boolean; render?: boolean; again?: boolean }) =>
  request<Job>('POST', `/api/kb/collections/${collectionId}/ingest/web`, body)
export const listChunks = (documentId: string, offset = 0) =>
  request<{ document: KbDocument; total: number; items: KbChunk[] }>('GET', `/api/kb/documents/${documentId}/chunks${q({ offset: offset ? String(offset) : undefined })}`)
export const editChunk = (id: string, text: string) => request<KbChunk>('PATCH', `/api/kb/chunks/${id}`, { text })
export const deleteChunk = (id: string) => request<void>('DELETE', `/api/kb/chunks/${id}`)
export const deleteDocument = (id: string) => request<void>('DELETE', `/api/kb/documents/${id}`)
export const addNote = (collectionId: string, title: string, text: string) =>
  request<KbDocument>('POST', `/api/kb/collections/${collectionId}/notes`, { title, text })
export const reembed = (collectionId: string, embedder: string) =>
  request<Job>('POST', `/api/kb/collections/${collectionId}/reembed`, { embedder })

// ---- MCP manager
export const mcpServers = () => request<{ servers: McpServer[]; error: string | null }>('GET', '/api/mcp/servers')
export const mcpInspect = (source: string) => request<McpInspection>('POST', '/api/mcp/inspect', { source })
export const mcpReview = (id: string) => request<McpReview>('POST', `/api/mcp/inspections/${id}/review`)
export const mcpInstall = (id: string, body: { name: string; risk: string; env: Record<string, string>; approved_install: string[][]; command?: string; args?: string[] }) =>
  request<McpServer>('POST', `/api/mcp/inspections/${id}/install`, body)
export const mcpAdd = (body: Partial<McpServer> & { name: string; env?: Record<string, string | null>; headers?: Record<string, string | null> }) => request<McpServer>('POST', '/api/mcp/servers', body)
export const mcpChange = (name: string, body: Partial<McpServer> & { env?: Record<string, string | null>; headers?: Record<string, string | null> }) =>
  request<McpServer>('PATCH', `/api/mcp/servers/${name}`, body)
export const mcpDelete = (name: string) => request<void>('DELETE', `/api/mcp/servers/${name}`)
export const mcpScan = (name: string) => request<McpScan>('GET', `/api/mcp/servers/${name}/scan`)
export const mcpApprove = (name: string, tools: Record<string, string>) => request<McpScan>('POST', `/api/mcp/servers/${name}/approve`, { tools })
export const mcpCheck = (name: string) => request<McpCheck>('POST', `/api/mcp/servers/${name}/check`)

// ---- personas
export type PersonaBody = Partial<Pick<Persona, 'name' | 'description' | 'system_prompt' | 'default_mode' | 'provider' | 'model' | 'tools' | 'collections'>>
export const listPersonas = () => request<Persona[]>('GET', '/api/personas')
export const createPersona = (body: PersonaBody) => request<Persona>('POST', '/api/personas', body)
export const changePersona = (id: string, body: PersonaBody) => request<Persona>('PATCH', `/api/personas/${id}`, body)
export const deletePersona = (id: string) => request<void>('DELETE', `/api/personas/${id}`)
export const personaTools = () => request<string[]>('GET', '/api/personas/tools')

// ---- memory
export const listMemories = (targetId?: string) =>
  request<{ total: number; items: Memory[]; settings: MemorySettings }>('GET', `/api/memories${q({ target_id: targetId })}`)
export const addMemory = (text: string, kind: Memory['kind'], targetId?: string | null) =>
  request<Memory>('POST', '/api/memories', { text, kind, target_id: targetId ?? null })
export const changeMemory = (id: string, text: string) => request<Memory>('PATCH', `/api/memories/${id}`, { text })
export const deleteMemory = (id: string) => request<void>('DELETE', `/api/memories/${id}`)
export const putMemorySettings = (patch: Partial<Pick<MemorySettings, 'enabled' | 'recall'>>) =>
  request<MemorySettings>('PUT', '/api/memories/settings', patch)
export const memorizeChat = (chatId: string) => request<Job>('POST', `/api/chats/${chatId}/memorize`)

export const systemStatus = () => request<SystemStatus>('GET', '/api/system/status')
export const systemCheckup = () => request<Checkup>('GET', '/api/system/checkup')
export const caidoStatus = () => request<CaidoStatus>('GET', '/api/caido/status')
export const caidoStart = () => request<CaidoStatus>('POST', '/api/caido/start')
export const caidoRequests = (filter: string, limit = 50) =>
  request<{ total: number; rows: CaidoRow[] }>('GET', `/api/caido/requests?limit=${limit}&filter=${encodeURIComponent(filter)}`)
export const caidoRequest = (id: string) => request<CaidoExchange>('GET', `/api/caido/requests/${encodeURIComponent(id)}`)
export const notifyTest = () => request<{ ok: boolean; detail: string }>('POST', '/api/notify/test')
export const activity = (days: number) => request<ActivitySummary>('GET', `/api/activity?days=${days}`)
export const listQueue = (chatId: string) => request<QueuedMessage[]>('GET', `/api/chats/${chatId}/queue`)
export const addQueue = (chatId: string, text: string) => request<QueuedMessage>('POST', `/api/chats/${chatId}/queue`, { text })
export const removeQueue = (chatId: string, id: string) => request<void>('DELETE', `/api/chats/${chatId}/queue/${id}`)
export const takeQueue = (chatId: string) => request<QueuedMessage[]>('POST', `/api/chats/${chatId}/queue/take`)
