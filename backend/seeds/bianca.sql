-- Bianca AI workspace and co-founders. Safe to re-run.

insert into workspaces (name, internal_domains, timezone)
values ('Bianca AI', '{bianca-ai.com}', 'America/Argentina/Buenos_Aires')
on conflict (name) do update
    set internal_domains = excluded.internal_domains,
        timezone = excluded.timezone;

insert into team_members (workspace_id, owner_code, full_name, email, receives_brief)
select w.id, t.owner_code, t.full_name, t.email, t.receives_brief
from workspaces w
cross join (values
    ('NC',  'Nicolás Casaux',  'nicolas.casaux@bianca-ai.com',  true),
    ('RG',  'Ramiro Godfrid',  'ramiro.godfrid@bianca-ai.com',  true),
    ('NDL', 'Nicolás de León', 'nicolas.deleon@bianca-ai.com',  false)
) as t(owner_code, full_name, email, receives_brief)
where w.name = 'Bianca AI'
on conflict (workspace_id, owner_code) do update
    set full_name = excluded.full_name,
        email = excluded.email,
        receives_brief = excluded.receives_brief;
