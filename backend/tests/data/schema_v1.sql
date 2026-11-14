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
  updated_at text not null
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
  created_at text not null
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
  detail_json text not null default '{}'
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
