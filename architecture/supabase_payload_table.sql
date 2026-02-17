create table if not exists public.aprt_story_payloads (
  run_id text primary key,
  story_id text not null,
  status text not null,
  generated_at timestamptz not null default now(),
  payload jsonb not null
);

create index if not exists idx_aprt_story_payloads_story_id
  on public.aprt_story_payloads (story_id);

create index if not exists idx_aprt_story_payloads_generated_at
  on public.aprt_story_payloads (generated_at desc);
