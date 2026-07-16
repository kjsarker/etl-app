-- Run this once in the Supabase SQL Editor (Project: One Minute Loader).
-- Stores per-user, encrypted ETL destination credentials. The app encrypts
-- the config blob client-side (Fernet) before it ever reaches this table --
-- Supabase / anyone with DB access only ever sees ciphertext.

create table if not exists public.credentials_vault (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    connection_name text not null,
    provider_id text not null,
    encrypted_config text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, connection_name)
);

alter table public.credentials_vault enable row level security;

drop policy if exists "Users manage their own credentials" on public.credentials_vault;

create policy "Users manage their own credentials"
    on public.credentials_vault
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Keep updated_at current on every change.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists set_credentials_vault_updated_at on public.credentials_vault;

create trigger set_credentials_vault_updated_at
    before update on public.credentials_vault
    for each row
    execute function public.set_updated_at();
