"""Golden runs: re-run a fixed sample of real items through classify -> link -> extract and save a trace per run.

Read-only against the database (nothing is written back). Output goes to review/golden/ (git-ignored):
one trace-NN.json per run plus runs.json, which build_eval_sheet.py turns into the evals spreadsheet.

    uv run python -m evals.golden_runs            # pick the sample and run it
"""

import json
import random
import time
from pathlib import Path

from psycopg.rows import dict_row

from app import classifier, db, extractor, resolution
from app.config import settings
from app.ingest import _workspace_id
from app.pipeline import normalize_name
from app.process import _claude, _context

OUT = Path(__file__).resolve().parents[2] / "review" / "golden"
SEED = 972
# (source, category) -> how many units to sample. 20 in total; only units whose text was kept.
PLAN = [
    ("gmail", "customer", 3), ("gmail", "prospect", 3), ("gmail", "internal_sales", 1), ("gmail", "vendor", 1),
    ("gmail", "other", 1),
    ("notes", "customer", 3), ("notes", "prospect", 2), ("notes", "partner", 1), ("notes", "vendor", 1),
    ("calendar", "customer", 2), ("calendar", "prospect", 1), ("calendar", "other", 1),
    ("gmail", "partner", 1),  # calendar has only one customer meeting with text
]
RENDER = {"notes": lambda r: classifier.render_note(r[0]), "calendar": lambda r: classifier.render_event(r[0])}


class Recorder:
    """Wraps client.messages.parse to log tokens and seconds for each model call."""

    def __init__(self, client):
        self.client, self.calls = client, []

    def parse(self, **kw):
        t = time.monotonic()
        r = self.client.messages.parse(**kw)
        u = r.usage
        self.calls.append({"seconds": round(time.monotonic() - t, 1),
                           "tokens": u.input_tokens + (u.cache_read_input_tokens or 0)
                           + (u.cache_creation_input_tokens or 0) + u.output_tokens})
        return r

    @property
    def messages(self):
        return self


def pick(conn, workspace_id) -> list[dict]:
    rng = random.Random(SEED)
    units = []
    for source, category, n in PLAN:
        pool = conn.execute(
            "select source, thread_id, max(occurred_at) as last, sum(length(body)) as chars"
            " from source_records where workspace_id = %s and source = %s and status = 'classified'"
            " and classification->>'category' = %s and body is not null and length(body) > 80"
            " group by source, thread_id order by thread_id", (workspace_id, source, category)).fetchall()
        units += [{**u, "stored_category": category} for u in rng.sample(pool, min(n, len(pool)))]
    return units


def run_unit(conn, workspace_id, client, system_prompt, company_ids, team_prompt, unit) -> dict:
    records = conn.execute(
        "select id, external_id, occurred_at::text, subject, participants, body, meta, classification,"
        " rules_company_id, link_method, company_id from source_records"
        " where workspace_id = %s and source = %s and thread_id = %s order by occurred_at",
        (workspace_id, unit["source"], unit["thread_id"])).fetchall()
    text = RENDER.get(unit["source"], classifier.render_thread)(records)
    names = {str(v[1]): v[0] for v in company_ids.values()}
    steps, rec = [], Recorder(client)

    steps.append({"step": 1, "type": "tool", "name": "load_item", "output": f"{len(records)} record(s), "
                  f"{len(text)} chars, {unit['source']}", "seconds": 0.0})

    result = classifier.classify(rec, settings.anthropic_model, system_prompt, text, unit["source"])
    stored = classifier.check(result, text, {k: v[0] for k, v in company_ids.items()})
    steps.append({"step": 2, "type": "model", "name": "classify", **rec.calls[-1],
                  "output": {k: stored[k] for k in ("category", "company", "new_company", "confidence",
                                                     "evidence_quote", "flags")}})

    clf_company = str(company_ids[normalize_name(stored["company"])][1]) if stored["company"] else None
    rule = records[0]["rules_company_id"] and str(records[0]["rules_company_id"])
    final, method = resolution.decide(clf_company, rule)
    steps.append({"step": 3, "type": "tool", "name": "link_company", "seconds": 0.0,
                  "input": {"classifier": names.get(clf_company), "email_domain_rules": names.get(rule)},
                  "output": {"company": names.get(final), "method": method}})

    insights, dropped = [], 0
    if stored["is_commercial"] and final:
        deal = conn.execute(
            "select c.name as company, o.stage, o.owner_code from opportunities o join companies c"
            " on c.id = o.company_id where o.company_id = %s", (final,)).fetchone()
        extraction = extractor.extract(rec, settings.anthropic_model, team_prompt, deal, text)
        insights, dropped = extractor.validate(extraction, text)
        steps.append({"step": 4, "type": "model", "name": "extract_insights", **rec.calls[-1],
                      "output": f"{len(extraction.insights)} proposed"})
        steps.append({"step": 5, "type": "tool", "name": "check_quotes", "seconds": 0.0,
                      "output": f"{len(insights)} kept, {dropped} dropped (quote not found in text)"})
    else:
        steps.append({"step": 4, "type": "tool", "name": "skip_extract", "seconds": 0.0,
                      "output": "not commercial" if not stored["is_commercial"] else "no pipeline company"})

    return {
        "source": unit["source"], "thread_id": unit["thread_id"], "occurred_at": str(unit["last"])[:16],
        "input": text,
        "stored": {"category": unit["stored_category"], "company": names.get(str(records[0]["company_id"]))
                   if records[0]["company_id"] else None, "link_method": records[0]["link_method"]},
        "steps": steps,
        "output": {"classification": stored, "company": names.get(final), "link_method": method,
                   "insights": [{**i, "due_date": i["due_date"].isoformat() if i["due_date"] else None}
                                for i in insights], "dropped_quotes": dropped},
        "model": settings.anthropic_model,
        "versions": {"classifier": classifier.PROMPT_VERSION, "extractor": extractor.EXTRACTOR_VERSION},
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = _claude()
    with db.connect() as conn:
        workspace_id = _workspace_id(conn)
        conn.row_factory = dict_row
        system_prompt, company_ids = _context(conn, workspace_id)
        team = conn.execute("select owner_code, full_name, email from team_members where workspace_id = %s"
                            " order by owner_code", (workspace_id,)).fetchall()
        team_prompt = extractor.build_system_prompt(team)
        units = pick(conn, workspace_id)
        runs = []
        for n, unit in enumerate(units, start=1):
            saved = OUT / f"trace-{n:02d}.json"
            if saved.exists() and json.loads(saved.read_text())["thread_id"] == unit["thread_id"]:
                runs.append(json.loads(saved.read_text()))  # already run; reruns only add new units
                continue
            run = {"run": n, **run_unit(conn, workspace_id, client, system_prompt, company_ids, team_prompt, unit)}
            run["trace_file"] = saved.name
            saved.write_text(json.dumps(run, indent=1, ensure_ascii=False, default=str))
            runs.append(run)
            out = run["output"]
            print(f"{n:2d} {unit['source']:8s} {out['classification']['category']:14s} "
                  f"{out['company'] or '-':22s} insights={len(out['insights'])}")
    (OUT / "runs.json").write_text(json.dumps(runs, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
