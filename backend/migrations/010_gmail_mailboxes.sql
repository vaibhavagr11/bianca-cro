-- 010_gmail_mailboxes: more than one founder's inbox. Gmail's thread ids differ per inbox; when the same email
-- arrives from a second inbox it is stored once, and that inbox's thread id is linked to the stored thread here.
-- source_records.meta gains "mailboxes" (inboxes that hold the email) and "message_id" (RFC Message-ID).

create table gmail_thread_aliases (
    workspace_id        uuid not null references workspaces(id) on delete cascade,
    mailbox             text not null,          -- team owner code, e.g. 'NC'
    thread_id           text not null,          -- Gmail thread id in that inbox
    canonical_thread_id text not null,          -- thread id the conversation is stored under
    created_at          timestamptz not null default now(),
    primary key (workspace_id, mailbox, thread_id)
);

alter table gmail_thread_aliases enable row level security;
