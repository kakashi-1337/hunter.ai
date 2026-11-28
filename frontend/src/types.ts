export type Block =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string }
  | { type: 'image'; upload_id: string; mime: string }
  | { type: 'file'; upload_id: string; filename: string; text: string | null; truncated: boolean; size: number }
  | { type: 'tool_call'; id: string; name: string; arguments: Record<string, unknown> }
  | { type: 'tool_result'; tool_call_id: string; content: string; is_error: boolean }
  | SourcesBlock
  | WebSourcesBlock
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
  top_k: number                // only the K likeliest tokens are considered
  min_p: number                // tokens less likely than this share of the likeliest one are left out
  repeat_penalty: number       // above 1: tokens that stand in the last tokens are less likely; 1: none
  repeat_last_n: number        // how many tokens back the penalties look
  presence_penalty: number     // for a token that stands there at all
  frequency_penalty: number    // the more often it stands there
  seed: number                 // the same seed gives the same answer to the same request
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

/** What the two scope lists say about one host, address or URL: the verdict of the permission check, the line behind
 * it, and what stands in the way. */
export interface ScopeAnswer {
  host: string
  port: number | null
  path: string | null
  verdict: 'in' | 'out' | 'not_in' | 'no_list' | 'local'   // not_in: a call is asked about; no_list: no In scope line; local: this machine
  line: string | null       // the line that decides, as written: of In scope for "in", of Out of scope for "out"
  says: string
  /** Where a command that names this address gets another answer: a command is checked without the path. */
  command: { verdict: 'in' | 'out'; says: string } | null
  hints: string[]
  unreadable: ScopeProblem[]
}

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
  kind: 'model_call' | 'tool_call' | 'subagent' | 'permission' | 'fallback' | 'decision' | 'retrieval' | 'user_note' | 'context' | 'check' | 'cooling' | 'thinking'
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
  /** How far the window of a chat on a local model grows by itself, in tokens. 0: a window stays as it is set. */
  local_window_max: number
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
  interface_font: string              // a font of this computer that the interface is set in; '' for the system's
  /** A chat's collections are searched before every answer, unless the chat switched that off for itself. */
  kb_lookup?: boolean
  /** The web is asked before every answer, with words of the message. Off until the owner switches it on. */
  web_lookup?: boolean
  vision_model: string | null
  browser_via_caido: boolean
  answer_check: boolean
  /** A call that names a host of its target's out-of-scope list is not run. Missing from a server of before the setting. */
  scope_refusal?: boolean
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
  /** Where the owner's llama-server is, as it is reached ("http://127.0.0.1:9931"); "" while llama.cpp is not used.
   * Absent: the server that answers is from before it knew llama.cpp (it was updated and not started again). */
  llamacpp_url?: string
  /** A key is stored for it. It is never sent to the page. */
  llamacpp_key_set?: boolean
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
  /** The model writes its answer again (in a larger context window): `blocks` is what the message holds now. */
  | { type: 'answer_restarted'; blocks: Block[] }
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

/** One result of the web search HunterAI made before an answer. */
export interface WebSource { n: number; title: string; url: string; snippet: string; engine: string }
/** What that search gave the answer, with the words that were searched for. Never sent back to a model. */
export interface WebSourcesBlock { type: 'web_sources'; query: string; items: WebSource[]; results: number; error: string | null }
/** `keywords_only`: why the vector half of the search did not answer, when the keyword index answered alone. */
export interface SourcesBlock { type: 'sources'; query: string; items: Source[]; hits: number; error: string | null; keywords_only?: string | null }
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

/** `collection_ids` and `always_retrieve` are what is in effect. `follows`: nobody chose collections for this chat, so
 * it reads the ones named for every chat. */
export interface ChatKb { chat_id: string; collection_ids: string[]; always_retrieve: boolean; follows?: boolean }
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

/** A program and the memory it holds, in bytes. */
export interface Holder { name: string; bytes: number }

/** The machine as it is now, for the Live monitor. What could not be read is null, and a machine without an NVIDIA
 * card has no cards. Memory is in bytes, `load` in percent, `temp` and `limit` in °C, `power` in watts. */
export interface MonitorReading {
  cpu: { load: number | null; temp: number | null; limit: number | null }
  ram: { used: number; total: number; holders: Holder[] } | null
  cards: { name: string; load: number | null; temp: number | null; limit: number | null; power: number | null
    used: number | null; total: number | null; holders: Holder[] }[]
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
  /** The models a llama-server holds in memory now, each with its context: none for a router that has loaded none.
   * Null: no address is set, or what is there serves no model to chat with. Absent: a server from before it asked. */
  llamacpp?: { model: string; context: number | null }[] | null
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

/** One model a llama-server lists. */
export interface LlamaModel {
  id: string
  context: number | null       // the tokens one request to it has; null when the server does not say
  images: boolean              // it reads pictures
  loaded: boolean              // it is in memory now (a router loads a model when a chat asks for it)
  failed: boolean              // the router tried to load it and could not
  enough: number | null        // the context to start llama-server with, when this one leaves no room for a chat
}

/** What the llama-server at the address in Settings says of itself, for the lines under that address. */
export interface LlamaServer {
  url: string
  /** off: no address is set. down: nothing answers. loading: it is still loading its model. key: it wants a key it
   * was not given. odd: what answers is not llama-server. nomodel: it has no model to chat with. ok: `models`. */
  state: 'off' | 'down' | 'loading' | 'key' | 'odd' | 'nomodel' | 'ok'
  said: string                 // why it serves nothing to chat with, in words; '' for off and ok
  router: boolean              // it was started with a folder of models and loads the one a chat asks for
  models: LlamaModel[]
  tools: boolean | null        // the chat template of a server with one model has tool calls; null: it does not say
  overhead: number             // what a new chat sends before the conversation, in tokens
  start_with: number           // the smallest context (-c) that holds that with room for a chat
}

/** A graphics card a llama-server says it can put a model on, with its memory in MiB. */
export interface LlamaDevice { name: string; card: string; total_mb: number; free_mb: number }

/** A chat model of Ollama as Settings offers it for llama-server. command: what starts llama-server with its files;
 * null when there is none, and `why_not` then says why. images: llama-server will read pictures with it. */
export interface LlamaFindModel { name: string; alias: string; images: boolean; command: string | null; why_not: string; note: string }

/** What this machine has to start a llama-server with (GET /api/system/llamacpp/find). */
export interface LlamaFind {
  /** Where a llama-server started here answers; null: the saved address is not one to start a server at here. */
  address: string | null
  program: string | null       // the llama-server that was found; null: none
  from_ollama: boolean         // it is the one Ollama brings
  devices: LlamaDevice[] | null        // the cards it sees; null: it does not say
  /** What answers at `address` now, looked at while no address is saved; null: nothing, or an address is saved. */
  answers: { state: 'ok' | 'loading' | 'key' | 'nomodel' | 'odd'; serves: string[]; said: string } | null
  context: number
  ollama: boolean              // Ollama answers
  held: string[]               // the chat models Ollama holds in memory now
  plain: string | null         // the command for a GGUF file of the owner's own
  models: LlamaFindModel[]
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

// ---- the bench: the same scripted tests for every model

export interface ModelRef {
  provider: string
  model: string
}

export interface BenchCaseBrief {
  id: string
  group: string
  /** The start of the prompt: a long one is cut. */
  prompt: string
  /** How the test is judged, in its own words; absent for a test that does not say it, and in a bench from before. */
  judge?: string | null
}

export interface BenchSet {
  id: string
  name: string
  builtin: boolean
  cases: BenchCaseBrief[]
  /** Why the file cannot be used; it has no cases then. */
  problem: string | null
  /** How many answers with a known verdict it has: a judge model is checked with them. Absent: a server from before them. */
  known?: number
}

export type BenchStatus = 'running' | 'done' | 'cancelled' | 'interrupted' | 'stopped'

export interface BenchRun {
  id: string
  set_id: string
  set_name: string
  repeats: number
  models: ModelRef[]
  /** The cases of the set as they were read when the bench started. */
  cases: BenchCaseBrief[]
  status: BenchStatus
  /** What the bench has to say: tools its set names that are not there, and why it ended early. */
  note: string
  tools_offered: number
  /** Every call of it was asked without thinking. Absent: a server from before a bench could be started that way. */
  thinking_off?: boolean
  created_at: string
  ended_at: string | null
}

export type BenchMarkStatus = 'pass' | 'fail' | 'error' | 'skipped'

/** One run of a case on a model, as the table needs it. */
export interface BenchMark {
  provider: string
  model: string
  case_id: string
  repeat: number
  /** The mark the rules of the set gave. */
  status: BenchMarkStatus
  /** What the owner says the run is, where he set its mark right by hand. The table counts with it. */
  by_hand?: 'pass' | 'fail' | null
  /** What a judge model said of the answer, after the bench had ended. It changes no mark. */
  judged?: 'pass' | 'fail' | null
  /** A call of this run went to a host on the out-of-scope list of the test's target, and the model was told so as a
   * chat tells it. True: it then made no such call again and gave the answer the test asks for. Null or not there: no
   * call of the run was refused (or the run broke). It changes no mark. */
  heeded?: boolean | null
  turns: number
  input_tokens: number
  output_tokens: number
  duration_ms: number
}

export interface BenchCall {
  name: string
  /** The start of the arguments, as JSON text. */
  arguments: string
  /** What the model was told when a chat would not have run the call. */
  problem: string | null
  /** The out-of-scope hosts for which the scope guard of a chat would not have run it: `problem` is its refusal then. */
  refused?: string[]
}

/** One run with what it left behind. */
export interface BenchResult extends BenchMark {
  id: string
  run_id: string
  /** One line per rule that did not hold. */
  reasons: string[]
  calls: BenchCall[]
  /** How many calls the model made; `calls` holds the first of them. */
  calls_made: number
  answer: string
  /** The end of what the model thought in its last call; '' when it did not think. Absent: a server from before it kept any. */
  thinking?: string
  /** The judge's own sentence, and why it gave no verdict when it was asked and gave none. */
  judge_reason?: string
  judge_error?: string | null
  created_at: string
}

/** Where a running bench is. Positions count from 1. */
export interface BenchProgress {
  model: number
  models: number
  case: number
  cases: number
  repeat: number
  repeats: number
  done: number
  total: number
  doing: 'running' | 'cooling'
}

/** The newest bench of a model with a set, for the table over all stored benches: the bench, and the model's marks in it. */
export interface BenchModelLine {
  model: ModelRef
  run: BenchRun
  marks: BenchMark[]
}

export interface BenchListed {
  run: BenchRun
  progress: BenchProgress | null
  /** How far a judge is with this bench, while one reads it. */
  judging?: { done: number; total: number } | BenchJudging | null
}

/** The last time a judge model read the answers of a bench. */
export interface BenchJudging {
  run_id: string
  provider: string
  model: string
  status: 'running' | 'done' | 'cancelled' | 'interrupted' | 'stopped'
  note: string
  /** The answers it was to read, and those it has read. */
  total: number
  done: number
  created_at: string
  ended_at: string | null
  /** While it runs: whether it reads, or waits for the machine to cool before it asks a judge that runs here. */
  doing?: 'running' | 'cooling' | null
}

export interface BenchDetail extends BenchListed {
  marks: BenchMark[]
  judging?: BenchJudging | null
}

/** The last time a judge model was asked about the answers of a set whose right verdict is known. */
export interface JudgeCheck {
  id: string
  set_id: string
  set_name: string
  provider: string
  model: string
  status: 'running' | 'done' | 'cancelled' | 'interrupted' | 'stopped'
  note: string
  /** The known answers of the set, and those it was asked about. */
  total: number
  done: number
  /** Of those: the ones it called as they are known, the fails it called a pass, the passes it called a fail, and the
   * ones it gave no verdict on. */
  correct: number
  passed_wrongly: number
  failed_wrongly: number
  unanswered: number
  created_at: string
  ended_at: string | null
  doing?: 'running' | 'cooling' | null
}

/** One known answer of a check: what was known, and what the judge said. */
export interface CheckedAnswer {
  case_id: string
  /** Which known answer of its test, from 1. */
  n: number
  expected: 'pass' | 'fail'
  verdict: 'pass' | 'fail' | null
  reason: string
  error: string | null
  answer: string
}

export interface JudgeCheckDetail {
  check: JudgeCheck
  answers: CheckedAnswer[]
}

/** What a look for gone files found in a collection: the documents that were read from a folder of this machine. */
export interface KbGone {
  checked: number
  /** Documents whose file is gone, and the first of them by address. */
  gone: number
  first: string[]
  /** Documents of a source that is not there now (a drive that is not plugged in), and those sources. They are left alone. */
  away: number
  sources: string[]
  /** Documents whose folder stands in no queue entry any more: not judged. */
  unknown: number
}
