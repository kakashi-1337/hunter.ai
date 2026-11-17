create table if not exists targets (
  id text primary key,
  name text not null,
  notes text not null default '',
  scope_json text not null default '{"in":[],"out":[]}',
  created_at text not null
);

create table if not exists chats (
  id text primary key,
  target_id text references targets(id) on delete set null,
  title text not null,
  mode text not null,
  provider text not null,
  model text not null,
  options_json text not null default '{}',
  active_leaf_id text,
  created_at text not null,
  updated_at text not null,
  workdir text,
  permissions_json text not null default '{}',
  persona_id text
);
create index if not exists chats_target on chats(target_id, updated_at);

create table if not exists messages (
  id text primary key,
  chat_id text not null references chats(id) on delete cascade,
  parent_id text references messages(id) on delete cascade,
  role text not null,
  blocks_json text not null,
  provider text,
  model text,
  fallback_from text,
  status text not null default 'complete',
  created_at text not null
);
create index if not exists messages_chat on messages(chat_id);
create index if not exists messages_parent on messages(parent_id);

create virtual table if not exists messages_fts using fts5(
  message_id unindexed, chat_id unindexed, text
);

create table if not exists findings (
  id text primary key,
  target_id text not null references targets(id) on delete cascade,
  title text not null,
  severity text not null default 'info',
  status text not null default 'open',
  evidence text not null default '',
  chat_id text,
  step_id text,
  created_at text not null,
  triage_json text not null default '{}'
);

create table if not exists runs (
  id text primary key,
  chat_id text not null references chats(id) on delete cascade,
  user_message_id text,
  assistant_message_id text,
  status text not null,
  error text,
  started_at text not null,
  ended_at text
);
create index if not exists runs_chat on runs(chat_id, started_at);

create table if not exists steps (
  id text primary key,
  run_id text not null references runs(id) on delete cascade,
  seq integer not null,
  kind text not null,
  name text not null,
  status text not null,
  started_at text not null,
  ended_at text,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  detail_json text not null default '{}',
  parent_step_id text
);
create index if not exists steps_run on steps(run_id, seq);

create table if not exists uploads (
  id text primary key,
  chat_id text,
  filename text not null,
  mime text not null,
  size integer not null,
  sha256 text not null,
  path text not null,
  created_at text not null
);

create table if not exists settings (
  key text primary key,
  value_json text not null
);

create table if not exists chat_grants (
  chat_id text not null references chats(id) on delete cascade,
  tool text not null,
  target text not null,
  warned integer not null default 0,
  created_at text not null,
  primary key (chat_id, tool, target)
);

create table if not exists processes (
  id text primary key,
  chat_id text not null references chats(id) on delete cascade,
  run_id text,
  step_id text,
  pid integer,
  pgid integer,
  start_ticks integer,
  command text not null,
  cwd text not null,
  log_path text not null,
  background integer not null default 0,
  status text not null,              -- running | exited | killed | lost
  exit_code integer,
  started_at text not null,
  ended_at text
);
create index if not exists processes_chat on processes(chat_id, started_at);

create table if not exists goals (
  chat_id text primary key references chats(id) on delete cascade,
  objective text not null,
  criteria_json text not null default '[]',
  open_items_json text not null default '[]',
  updated_at text not null
);

create table if not exists subruns (
  id text primary key,
  run_id text not null references runs(id) on delete cascade,
  step_id text,
  brief text not null,
  blocks_json text not null default '[]',
  status text not null,
  verdict_json text not null default '{}',
  created_at text not null,
  ended_at text
);
create index if not exists subruns_run on subruns(run_id);

-- Phase 3: job queue and knowledge collections
create table if not exists jobs (
  id text primary key,
  kind text not null,
  state text not null,               -- queued | running | paused | done | failed | cancelled
  payload_json text not null default '{}',
  checkpoint_json text not null default '{}',
  attempts integer not null default 0,
  error text,
  progress_total integer not null default 0,
  progress_done integer not null default 0,
  stats_json text not null default '{}',
  created_at text not null,
  updated_at text not null,
  started_at text,
  finished_at text
);
create index if not exists jobs_state on jobs(state, created_at);

create table if not exists kb_collections (
  id text primary key,
  name text not null unique collate nocase,
  embedder text not null,
  dimension integer not null,
  created_at text not null
);

create table if not exists kb_documents (
  id text primary key,
  collection_id text not null references kb_collections(id) on delete cascade,
  source_kind text not null,
  source_uri text not null,
  sha256 text not null default '',
  size integer not null default 0,
  status text not null,              -- processing | ok | skipped | failed
  reason text,
  chunk_count integer not null default 0,
  job_id text,
  created_at text not null,
  updated_at text not null,
  unique(collection_id, source_uri)
);
create index if not exists kb_documents_status on kb_documents(collection_id, status, updated_at);

create table if not exists kb_chunks (
  n integer primary key,
  id text not null unique,
  document_id text not null references kb_documents(id) on delete cascade,
  collection_id text not null,
  ordinal integer not null,
  text text not null,
  char_start integer not null,
  char_end integer not null,
  line_start integer not null,
  line_end integer not null,
  embedded integer not null default 0
);
create index if not exists kb_chunks_doc on kb_chunks(document_id, ordinal);
create index if not exists kb_chunks_coll on kb_chunks(collection_id);

create virtual table if not exists kb_chunks_fts using fts5(text, content='kb_chunks', content_rowid='n');
create trigger if not exists kb_chunks_ai after insert on kb_chunks begin
  insert into kb_chunks_fts(rowid, text) values (new.n, new.text);
end;
create trigger if not exists kb_chunks_ad after delete on kb_chunks begin
  insert into kb_chunks_fts(kb_chunks_fts, rowid, text) values ('delete', old.n, old.text);
end;

create table if not exists kb_job_items (
  n integer primary key autoincrement,
  job_id text not null references jobs(id) on delete cascade,
  uri text not null,
  path text not null,
  size integer not null default 0,
  state text not null default 'pending',
  unique(job_id, uri)
);

create table if not exists chat_kb (
  chat_id text primary key references chats(id) on delete cascade,
  always_retrieve integer not null default 0
);
create table if not exists chat_kb_collections (
  chat_id text not null references chats(id) on delete cascade,
  collection_id text not null references kb_collections(id) on delete cascade,
  primary key(chat_id, collection_id)
);

create table if not exists kb_vector_collections (name text primary key, dim integer not null);
create table if not exists kb_vectors (
  collection text not null,
  chunk_id text not null,
  document_id text not null,
  vec blob not null,
  primary key(collection, chunk_id)
);
create index if not exists kb_vectors_doc on kb_vectors(collection, document_id);

create trigger if not exists kb_chunks_au after update of text on kb_chunks begin
  insert into kb_chunks_fts(kb_chunks_fts, rowid, text) values ('delete', old.n, old.text);
  insert into kb_chunks_fts(rowid, text) values (new.n, new.text);
end;

-- Phase 5: personas
create table if not exists personas (
  id text primary key,
  name text not null unique collate nocase,
  description text not null default '',
  system_prompt text not null,
  default_mode text,                  -- plan | pentesting | coding | null (keep the chat's)
  provider text,                      -- preferred model, or null (keep the chat's)
  model text,
  tools_json text not null default '[]',        -- tool names offered; empty = every tool
  collections_json text not null default '[]',  -- knowledge collections attached by default
  builtin integer not null default 0,
  created_at text not null,
  updated_at text not null
);

-- Skills: instructions for a kind of work, started with a slash command or loaded by the model
create table if not exists skills (
  id text primary key,
  name text not null unique collate nocase,   -- also the slash command: lower-case letters, digits, hyphens
  description text not null default '',       -- when to use it; the model reads this line
  body text not null,                         -- the instructions
  mode text,                                  -- plan | pentesting | coding | null: the mode its slash command switches to
  source text not null default '',            -- where it came from: an address, a folder, "pasted text", "built in"
  folder text,                                -- where its other files are kept, or null
  enabled integer not null default 1,
  auto integer not null default 1,            -- the model may load it by itself
  builtin integer not null default 0,
  created_at text not null,
  updated_at text not null
);

-- Phase 5: memory
create table if not exists memories (
  id text primary key,
  text text not null,
  kind text not null,                 -- fact | lesson | preference
  importance real not null default 0.5,
  chat_id text,                       -- where it came from; kept after the chat is deleted
  target_id text,
  message_id text,
  document_id text,                   -- its entry in the memory collection
  created_at text not null,
  updated_at text not null
);
create index if not exists memories_target on memories(target_id, created_at);
create table if not exists memory_state (
  chat_id text primary key references chats(id) on delete cascade,
  leaf_id text not null,
  extracted_at text not null,
  failed_leaf text,                   -- the leaf an extraction last failed on
  retry_after text                    -- not tried again on that leaf before this time
);

create table if not exists queued_messages (
  id text primary key,
  chat_id text not null references chats(id) on delete cascade,
  text text not null,
  created_at text not null
);
create index if not exists queued_messages_chat on queued_messages(chat_id, created_at);

-- What stands in for the older part of a chat that no longer fits its model's context window.
create table if not exists chat_summaries (
  chat_id text not null references chats(id) on delete cascade,
  upto_message_id text not null,       -- the summary covers the chat's path up to and including this message
  summary text not null,
  created_at text not null,
  primary key (chat_id, upto_message_id)
);
