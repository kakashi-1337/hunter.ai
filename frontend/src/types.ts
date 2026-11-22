export type Block =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string }
  | { type: 'image'; upload_id: string; mime: string }
  | { type: 'file'; upload_id: string; filename: string; text: string | null; truncated: boolean; size: number }
  | { type: 'tool_call'; id: string; name: string; arguments: Record<string, unknown> }
  | { type: 'tool_result'; tool_call_id: string; content: string; is_error: boolean }
  | SourcesBlock
  | AnswerCheckBlock
  | { type: 'owner_note'; text: string }
  /** Something about the answer the owner has to know and its text does not show: it was cut off at a limit, or the
   * provider's filter ended it. Never sent to a model. */
  | { type: 'notice'; kind: string; text: string }
  | ChangesBlock

/** What a run in coding mode changed in its work folder, with the save points that let the owner put it back.
 * `reason` says why there is no list (no save point could be kept). Never sent to a model. */
export interface ChangesBlock {
  type: 'changes'
  run_id: string
  folder: string
  files: { path: string; change: 'new' | 'changed' | 'deleted' }[]
  before: string
  after: string
  reason: string
  /** The check of the folder before and after the run, when the owner set one for the folder. */
  check?: FolderCheck | null
  /** Every file is as it was before the run again: nothing is left to undo. */
  undone: boolean
  /** After an undo: the files that changed after the run and were left as they are. The undo can be asked again. */
  left: string[]
}

/** One run of a folder's check: the command the owner set for the folder (the project's tests). */
export interface CheckResult { passed: boolean; exit_code: number | null; seconds: number; timed_out: boolean; output: string }
/** `after` is null when no check followed the run; `note` then says why. */
export interface FolderCheck { command: string; before: CheckResult | null; after: CheckResult | null; note: string }

export type MessageStatus = 'complete' | 'streaming' | 'error' | 'interrupted' | 'cancelled'
export type Mode = 'plan' | 'pentesting' | 'coding'

export interface Message {
  id: string
  chat_id: string
  parent_id: string | null
  role: 'user' | 'assistant' | 'system'
  blocks: Block[]
  provider: string | null
  model: string | null
  fallback_from: string | null
  status: MessageStatus
  created_at: string
  siblings: string[]
}

export type Options = Partial<{
  temperature: number
  top_p: number
  max_tokens: number
  context_window: number
  effort: 'none' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
  thinking: boolean
  thinking_budget: number
  verbosity: 'low' | 'medium' | 'high'
}>

export interface Chat {
  id: string
  target_id: string | null
  title: string
  mode: Mode
  provider: string
  model: string
  options: Options
  active_leaf_id: string | null
  created_at: string
  updated_at: string
  workdir: string | null
  permissions: Policy
  persona_id?: string | null
}

export type Risk = 'read' | 'write' | 'execute' | 'network'
export type Decision = 'allow' | 'ask' | 'deny'

/** One shape for the global defaults and for a chat's own settings. */
export type Policy = Partial<{
  risk: Partial<Record<Risk, Decision>>
  tools: Record<string, Decision>
  read_paths: string[]
  write_paths: string[]
  domains: string[]
  /** A chat's own switch: do not ask where only the mode or a default says "ask". */
  auto: boolean
}>

export interface PermissionRequest {
  id: string
  run_id: string
  chat_id: string
  tool_call_id: string
  tool: string
  risk: string
  source: string
  arguments: Record<string, unknown>
  cwd: string | null
  paths: { path: string; mode: 'read' | 'write' }[]
  hosts: { host: string; port: number | null; ips: string[] }[]
  preview: string | null
  reasons: string[]
  warnings: string[]
  can_allow_chat: boolean
  grant_target: string
  gate: Record<string, unknown> | null
  created_at: string
}

export type PermissionDecision = 'allow_once' | 'allow_chat' | 'deny'

export interface Grant { tool: string; target: string; warned: boolean }

export interface ToolInfo {
  name: string
  description: string
  risk: Risk
  source: string
  offered: boolean
  decision: Decision
  rule: string
}

export interface ProcessInfo {
  id: string
  chat_id: string
  pid: number | null
  command: string
  cwd: string
  background: boolean
  status: 'running' | 'exited' | 'killed' | 'lost'
  exit_code: number | null
  started_at: string
  ended_at: string | null
}

export interface McpStatus {
  configured: { name: string; enabled: boolean; risk: Risk; transport: string; expose: boolean; bundled: boolean }[]
  servers: { name: string; transport: string; state: string; tools: string[]; held?: string[]; error: string | null; restarts: number; forced_restarts: number }[]
  error: string | null
}

/** A line of a stored scope that the scope check cannot read: it is checked against nothing until it is rewritten. */
export interface ScopeProblem { side: 'in' | 'out'; line: string; why: string }

export interface Target {
  id: string
  name: string
  notes: string
  scope: { in: string[]; out: string[] }
  scope_problems?: ScopeProblem[]
  /** This target's own proxy as it is shown: with the user name, never with the password. "" for none. */
  proxy?: string
  proxy_password?: boolean
  created_at: string
}

/** What the test of a proxy found: whether it carried a request, and the address and country sites see. */
export interface ProxyTest {
  ok: boolean
  through: string
  ms: number
  ip: string
  country: string
  error: string
}

/** What a pasted program brief says about scope: read by rules on the server, shown for review, never saved by itself. */
export interface BriefResult {
  name: string
  in: string[]
  out: string[]
  unplaced: string[]      // hosts the text names without a side
  unsupported: string[]   // patterns no scope entry can say, as written
  notes: string
}

export interface Step {
  id: string
  run_id: string
  seq: number
  kind: 'model_call' | 'tool_call' | 'subagent' | 'permission' | 'fallback' | 'decision' | 'retrieval' | 'user_note' | 'context' | 'check' | 'cooling'
  parent_step_id?: string | null
  name: string
  status: string
  started_at: string
  ended_at: string | null
  input_tokens: number
  output_tokens: number
  detail: Record<string, unknown>
}

export interface Totals {
  duration_ms: number
  turns: number
  calls: number
  input_tokens: number
  output_tokens: number
}

export interface RunSummary {
  id: string
  chat_id: string
  user_message_id: string | null
  assistant_message_id: string | null
  status: 'running' | 'done' | 'error' | 'cancelled' | 'interrupted'
  error: string | null
  started_at: string
  ended_at: string | null
  totals: Totals
}

export interface RunDetail extends RunSummary {
  steps: Step[]
}

export interface ModelInfo {
  id: string
  label: string
  supports_images: boolean
  options: string[]
  context_window: number | null
  /** The effort values this model takes, when they are fewer than the usual five. */
  efforts?: string[]
}

export interface ProviderInfo {
  name: string
  configured: boolean
  models: ModelInfo[]
  /** Why it is set up and offers no model, when it knows (colibri: nothing answers at its address). */
  note?: string
}

export interface Settings {
  default_provider: string
  default_model: string
  fallback_model: string
  /** Tokens of context every call to a local (Ollama) model gets, unless the chat sets its own. */
  local_window: number
  keys: Record<string, boolean>
  permissions: Policy
  max_turns: number
  /** Subagents of one swarm that work at the same time: 1 to swarm_width_max. */
  swarm_width: number
  swarm_width_max: number
  /** What this machine has; null for what could not be read. */
  machine: { cores: number | null; memory_gb: number | null }
  presets: Record<string, Record<Risk, Decision>>
  decision_provider: string | null
  decision_model: string | null
  decision_gate: boolean
  decision_engine: boolean
  kb_redact: boolean
  /** Build output, bundles, source maps and lock files are left out of a collection when a folder is indexed. */
  kb_skip_generated: boolean
  vision_model: string | null
  browser_via_caido: boolean
  answer_check: boolean
  mcp_on_demand: boolean
  notify_findings: boolean
  notify_min_severity: Severity
  notify_webhook_set: boolean
  caido_available: boolean
  /** The owner's proxy for the agent's traffic, as it is shown: "http://host:port" or "socks5://user@host:port"; "" for none. */
  upstream_proxy: string
  /** A password is stored with it. It is never sent to the page. */
  upstream_proxy_password: boolean
  /** It reads https and presents its own certificates (Burp, mitmproxy). Only honoured for a proxy on this machine. */
  upstream_proxy_intercepts: boolean
  /** Knowledge crawls go through that proxy too. Off: they connect directly. */
  upstream_proxy_kb: boolean
  /** The proxy the model providers are reached through, as it is shown; "" for none. */
  provider_proxy: string
  provider_proxy_password: boolean
  /** Where the owner's colibri server is, as it is reached ("http://127.0.0.1:8000"); "" while colibri is not used.
   * Absent: the server that answers is from before it knew colibri (it was updated and not started again). */
  colibri_url?: string
  /** A key is stored for it. It is never sent to the page. */
  colibri_key_set?: boolean
  /** The embedding model of the knowledge base and of memory runs on the processor, not on the graphics card.
   * Absent: a server from before it knew of this. */
  embed_on_cpu?: boolean
  /** The cloud model that does the subagent work of a chat on a local model. Null: such a chat has no subagents and
   * its model works alone. `subagent_providers`: the providers that can be chosen, cloud ones that use tools. All
   * three are absent from a server from before it knew of this. */
  subagent_provider?: string | null
  subagent_model?: string | null
  subagent_providers?: string[]
}

/** What the check said about one item of a goal record. Only `shown` makes an item done: the decision model found the
 * work in a tool result of the chat, and the words it quoted really stand in that result. */
export interface GoalMark { verdict: string; evidence: string; tool: string; model: string; at: string }

export interface Goal {
  chat_id: string
  objective: string
  criteria: string[]
  open_items: string[]
  done_items?: string[]
  marks?: Record<string, GoalMark>
  updated_at: string
}

/** One helper of a run as the goal panel lists it: a delegate call or a member of a swarm. */
export interface Helper { id: string; brief: string; state: 'queued' | 'running' | 'done' | 'error' | 'cancelled'; verified?: boolean }

export type Severity = 'info' | 'low' | 'medium' | 'high' | 'critical'
export type FindingStatus = 'open' | 'confirmed' | 'false_positive' | 'duplicate' | 'reported' | 'fixed'

export interface Finding {
  id: string
  target_id: string
  title: string
  severity: Severity
  status: FindingStatus
  evidence: string
  chat_id: string | null
  step_id: string | null
  triage: Record<string, unknown>
  created_at: string
}

export interface Upload {
  id: string
  chat_id: string | null
  filename: string
  mime: string
  size: number
  sha256: string
  created_at: string
  /** Said once, in the answer to the upload: why a model will not get this file as it is (a picture that cannot be decoded). */
  problem?: string
}

export interface SearchHit {
  chat_id: string
  chat_title: string
  message_id: string
  snippet: string
}

export interface StartedRun {
  run_id: string
  user_message_id: string
  assistant_message_id: string
}

export type RunEvent =
  | { type: 'run_started'; run_id: string; assistant_message_id: string }
  | { type: 'step_started'; step: Step }
  | { type: 'step_finished'; step: Step }
  | { type: 'text_delta'; text: string }
  | { type: 'thinking_delta'; text: string }
  | { type: 'tool_call'; id: string; name: string; arguments: Record<string, unknown>; error: string | null }
  | { type: 'tool_started'; tool_call_id: string; step_id: string }
  | { type: 'tool_finished'; tool_call_id: string; content: string; is_error: boolean; duration_ms: number }
  | { type: 'permission_request'; request: PermissionRequest }
  | { type: 'permission_resolved'; request_id: string; tool_call_id: string; answer: PermissionDecision | 'cancelled' | 'interrupted'; note: string }
  | { type: 'fallback'; from_provider: string; from_model: string; to_provider: string; to_model: string; reason: string }
  | { type: 'run_finished'; status: RunSummary['status']; error: string | null; totals: Totals; message: Omit<Message, 'siblings'> | null }

// ---- knowledge base

export interface Source {
  n: number
  chunk_id: string
  document_id: string
  collection_id: string
  source_uri: string
  ordinal: number
  line_start: number
  line_end: number
  score: number
  text: string
  /** It stood first because it holds an identifier of the query in its exact spelling. Its score is then no measure. */
  exact?: boolean
  /** The half of the search that had it first: it leads the fused list for that, and its score is no measure either. */
  first_of?: 'vectors' | 'keywords' | null
  /** Passages its document was cut into: this one is part `ordinal + 1` of them. */
  parts?: number | null
  /** The title and the headings it stands under in its document, as the model was shown them. The document's own words. */
  where?: string
}

export interface SourcesBlock { type: 'sources'; query: string; items: Source[]; hits: number; error: string | null }
export interface CheckedClaim { claim: string; verdict: 'unsupported' | 'contradicted'; evidence: string }
/** What the decision model found when it held the final answer against the run's tool results. */
export interface AnswerCheckBlock { type: 'answer_check'; claims: CheckedClaim[]; checked: number; supported: number; model: string }

export interface Collection { id: string; name: string; embedder: string; dimension: number; created_at: string }

export type DocStatus = 'processing' | 'ok' | 'skipped' | 'failed'

export interface KbDocument {
  id: string
  collection_id: string
  source_kind: string
  source_uri: string
  sha256: string
  size: number
  status: DocStatus
  reason: string | null
  chunk_count: number
  job_id: string | null
  created_at: string
  updated_at: string
}

export type JobState = 'queued' | 'running' | 'paused' | 'done' | 'failed' | 'cancelled'
export type JobAction = 'pause' | 'resume' | 'cancel' | 'retry'

export interface Job {
  id: string
  kind: string
  state: JobState
  payload: { collection_id?: string; staging_id?: string; source?: { kind: string; path?: string; url?: string } }
  checkpoint: { phase?: string; stage?: string; uri?: string }
  attempts: number
  error: string | null
  progress_total: number
  progress_done: number
  stats: Record<string, number | null>
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
}

export interface KbStatus {
  collection: Collection
  documents: Record<DocStatus, number>
  chunks: number
  vectors: number | null
  vector_error: string | null
  dimension: number
  embedder: string
  active_job: Job | null
  embed_rate: number | null
  eta_seconds: number | null
}

export interface KbHit {
  chunk_id: string
  document_id: string
  collection_id: string
  source_uri: string
  ordinal: number
  text: string
  line_start: number
  line_end: number
  score: number
  dense_rank: number | null
  keyword_rank: number | null
  /** It holds an identifier of the query in its exact spelling, and stands first for that. */
  exact?: boolean
  /** The half of the search that has it first: it leads the fused list for that. */
  first_of?: 'vectors' | 'keywords' | null
  /** Passages its document was cut into. */
  parts?: number | null
  where?: string
}

export interface ChatKb { chat_id: string; collection_ids: string[]; always_retrieve: boolean }
export interface KbHealth { worker_alive: boolean; vector_backend: string }
export interface UploadResult { accepted: number; rejected: { path: string; reason: string }[]; files: number; bytes: number }

export interface KbChunk {
  id: string
  document_id: string
  collection_id: string
  ordinal: number
  text: string
  char_start: number
  char_end: number
  line_start: number
  line_end: number
  embedded: boolean
}

// ---- MCP manager

export interface McpServer {
  name: string
  transport: 'stdio' | 'http'
  command: string | null
  args: string[]
  cwd: string | null
  url: string | null
  risk: Risk
  enabled: boolean
  expose: boolean
  bundled: boolean
  env_names: string[]
  header_names?: string[]
}

export interface McpFinding { rule: string; severity: 'high' | 'medium' | 'info'; title: string; file: string; line: number; excerpt: string }

export interface McpInspection {
  id: string
  source: string
  kind: 'git' | 'npm' | 'pypi' | 'folder' | 'config'
  label: string | null
  root: string | null
  proposal: {
    name: string; transport: 'stdio' | 'http'; command: string | null; args: string[]; url: string | null; cwd: string | null
    env: Record<string, string>; env_needed: string[]; install: string[][]; install_runs_code?: (string | null)[]; notes: string[]
  }
  report: { findings: McpFinding[]; files: number; read: number; not_unpacked?: string[]; bytes: number; truncated: boolean; counts: Record<string, number>; more: Record<string, number> } | null
  audit: { checked: number; vulnerable: { ecosystem: string; name: string; version: string; ids: string[] }[]; error: string | null; note?: string } | null
  verdict: { level: 'danger' | 'caution' | 'nothing-found' | 'unchecked'; text: string }
  review?: McpReview
}

export interface McpReview { invalid: boolean; model: string; malicious_probability: number; advice: 'install' | 'read_first' | 'do_not_install'; advice_confidence: number; error?: string | null }

export interface McpScanFinding { rule: string; severity: 'high' | 'medium' | 'info'; title: string; where: string; excerpt: string }
export interface McpScanTool { name: string; digest: string; state: 'ok' | 'flagged' | 'new' | 'changed'; held: boolean; findings: McpScanFinding[]; description: string; schema?: string; previous?: string; previous_schema?: string }
export interface McpScanReport { server: string; first_sight: boolean; exposed: boolean; scanned_at: number; tools: McpScanTool[]; held: number; removed: string[]; note?: string }
export interface McpScan { name: string; state: string; report: McpScanReport | null }

export interface McpCheck { name: string; state: string; tools: string[]; error: string | null; ping_ms: number | null; log_tail: string; hints: string[] }

/** Instructions for a kind of work. Its name is its slash command; the model follows it as the owner's own words. */
export interface Skill {
  id: string
  name: string
  description: string
  body: string
  mode: Mode | null         // the mode its slash command switches the chat to first
  source: string
  folder: string | null
  files: string[]           // its other files, which the model can read
  enabled: boolean
  auto: boolean             // the model may load it by itself
  builtin: boolean
  created_at: string
  updated_at: string
}

export interface SkillFinding { rule: string; severity: 'high' | 'medium' | 'info'; title: string; where: string; excerpt: string }

/** One skill found in a source that was looked at. Nothing is kept until it is added. */
export interface SkillCandidate {
  key: string
  name: string
  description: string
  mode: Mode | null
  body: string
  files: string[]
  left_out: string[]        // files that do not come along: links, and whatever is over the limits
  findings: SkillFinding[]
  taken: boolean
  problem: string | null
}

export interface SkillInspection { id: string; label: string; skills: SkillCandidate[] }

export interface Persona {
  id: string
  name: string
  description: string
  system_prompt: string
  default_mode: Mode | null
  provider: string | null
  model: string | null
  tools: string[]
  collections: string[]
  builtin: boolean
  created_at: string
  updated_at: string
}

export interface Memory {
  id: string
  text: string
  kind: 'fact' | 'lesson' | 'preference'
  importance: number
  chat_id: string | null
  target_id: string | null
  message_id: string | null
  document_id: string | null
  created_at: string
  updated_at: string
}
export interface MemorySettings { enabled: boolean; recall: boolean; collection_id: string | null; idle_seconds: number }

export interface FindingRow extends Finding {
  target_name: string
  chat_title: string | null
}

export interface SystemStatus {
  cpu_temp: number | null
  cpu_limit: number | null
  /** The graphics card, like the processor. `gpu_temp` is null where the card cannot be read, and while no local
   * model is loaded: asking wakes a card that a laptop has put to sleep. Absent: a server from before it read the card. */
  gpu_temp?: number | null
  gpu_limit?: number | null
  /** null: Ollama does not answer. `size_mb`: all of the model in memory; `vram_mb`: the part on the GPU; `processor`:
   * where it runs, in the words of `ollama ps`; `context`: the window it was loaded with. The last three are absent
   * from a server that could not tell yet. */
  local_models: { name: string; vram_mb: number; size_mb?: number; processor?: string; context?: number | null }[] | null
  /** The model a colibri server holds now, with its context. Null: no address is set, or what is there serves no
   * model to chat with. Absent: a server from before it asked colibri. */
  colibri?: { model: string; context: number | null } | null
  /** The server's code changed on disk after it started. Absent: a server from before it could tell. */
  restart_needed?: boolean
  /** The parts that were in use and do not run now: a service beside HunterAI that answered within the last week,
   * an MCP server that is switched on and gave up. Absent: a server from before it could tell. */
  stopped?: { id: string; label: string }[]
  /** The script the built interface starts with now. Null: nothing is built. Absent: a server from before it could tell. */
  page?: string | null
}

/** The local model in use (the fallback model) and its window, for the hint beside the window setting. */
export interface LocalModel {
  answers: boolean             // Ollama answers
  model: string
  installed: boolean
  window: number               // what a call to it gets now
  limit: number | null         // the most tokens the model takes
  bytes_per_token: number | null   // memory for one token of window, from the layers of the model
  overhead: { tokens: number; tools: number; on_demand: number }   // what a new chat sends before the conversation
  enough: number               // the window that leaves room for a chat beside that
  loaded: { processor: string; context: number | null; size_mb: number } | null
}

/** What the colibri server at the address in Settings says of itself, for the line under that address. */
export interface ColibriServer {
  url: string
  /** off: no address is set. down: nothing answers. key: it wants a key it was not given. refused: it refused who
   * asked. odd: what answers is not colibri. nochat: its model does not chat. ok: it serves `model`. */
  state: 'off' | 'down' | 'key' | 'refused' | 'odd' | 'nochat' | 'ok'
  said: string                 // why it serves nothing to chat with, in words; '' for off and ok
  model: string | null
  context: number | null       // the tokens of context it holds; null when it does not say
  images: boolean              // the model reads pictures
  overhead: number | null      // what a new chat sends before the conversation, in tokens
  enough: number | null        // the context to start it with, when its own leaves no room for a chat
}

export interface CheckupItem {
  id: string; label: string; ok: boolean; required: boolean; detail: string; fix: string
  /** It was in use and does not run now. Absent: a server from before it could tell. */
  stopped?: boolean
}
export interface Checkup { ready: boolean; items: CheckupItem[] }

export interface CaidoStatus {
  available: boolean
  running: boolean
  url?: string
  version?: string | null
  project?: string | null
  temporary?: boolean
  requests?: number | null
  managed?: boolean
  error?: string
}
export interface CaidoRow { id: string; method: string; url: string; status: number | null; length: number | null; ms: number | null; source: string | null; at: number | string | null }
export interface CaidoExchange { id: string; request: string; response: string | null }

export interface SwarmMember {
  n: number
  brief: string
  state: 'queued' | 'running' | 'done' | 'error' | 'cancelled'
  verified?: boolean
  verdict?: string
  ms?: number
  turns: number
  calls: number
  answer?: string
}

export interface ActivitySummary {
  days_asked: number
  totals: { runs: number; model_calls: number; tool_calls: number; input_tokens: number; output_tokens: number; errors: number }
  models: { name: string; calls: number; errors: number; input_tokens: number; output_tokens: number; avg_seconds: number }[]
  tools: { name: string; calls: number; errors: number; avg_seconds: number }[]
  decisions: { calls: number; errors: number; avg_seconds: number }
  days: { day: string; runs: number; model_calls: number; tool_calls: number; input_tokens: number; output_tokens: number; errors: number }[]
  log_file: string
}

/** A message the owner sent while a run was in progress: it waits for the agent's next step. */
export interface QueuedMessage { id: string; chat_id: string; text: string; created_at: string }
