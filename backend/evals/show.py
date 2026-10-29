"""Show the product and its failure-mode checks live, for class.

Product run (read-only against production):
    uv run python -m evals.show run-log            what this morning's (or the latest) processing run did, step by step
    uv run python -m evals.show item [N]           the N most recently processed items: stored -> classified -> linked
                                                   -> extracted -> quotes re-checked now
    uv run python -m evals.show brief              build today's brief as a dry run on Render (nothing stored or sent)
                                                   and open it in the browser

Failure modes:
    uv run python -m evals.show summary
    uv run python -m evals.show run 24 [--only FM2,FM3] [--live] [--no-judge] [--save]

summary reads review/failure_modes.json (no AI, no database). run reads the team's eval sheet and the cached v2 output;
--live re-runs today's pipeline on the input first; the AI judge is called live unless --no-judge; --only limits the checks
shown; --save also writes the output to review/demo_runs/run-NN.txt as a backup. Email addresses and passcodes are masked in what is printed.
"""

import json
import re
import sys
from pathlib import Path

REVIEW = Path(__file__).resolve().parents[2] / "review"
SHEET = REVIEW / "Bianca Eval - Week 5 submitted.xlsx"
VERSIONS = {"v0": "n8n prototype", "v1": "Claude backend, Oct 21", "v2": "today's backend"}
FAILURE_MODES = [  # key, name, graded by
    ("FM1", "Sales relevance misclassification", "code vs team labels"),
    ("FM2", "Missing actions or follow-ups", "AI judge"),
    ("FM3", "Incorrect or duplicate event type", "code + AI judge"),
    ("FM4", "Sensitive input exposure", "code"),
    ("FM5", "Merged meeting inputs", "code"),
    ("B1", "Brief: item in the wrong founder's section", "code"),
    ("B2", "Brief: stale item shown as \"do today\"", "code"),
    ("B3", "Brief: active prospect missing", "code"),
]
EMAIL = re.compile(r"<?[\w.+-]+@[\w-]+(?:\.[\w-]+)+>?")
PASS, FAIL = "PASS", "FAIL"


def summary() -> None:
    res = json.loads((REVIEW / "failure_modes.json").read_text())
    print(f"Failure-mode checks (run {res['ran_at']} UTC) — failing / checked, lower is better\n")
    print(f"{'':4} {'Failure mode':44} {'Graded by':20} {'v0 n8n':>8} {'v1 Oct21':>9} {'v2 today':>9}")
    for key, name, grader in FAILURE_MODES:
        r = res.get(key)
        if r:
            cells = [f"{r[v]['fail']}/{r[v]['of']}" if r.get(v) else "–" for v in ("v0", "v1", "v2")]
            print(f"{key:4} {name:44} {grader:20} {cells[0]:>8} {cells[1]:>9} {cells[2]:>9}")
    if "FM2" in res:
        print("\nFM2 action coverage (judge's Bianca actions that got a to-do):")
        for k, v in res["FM2"]["coverage"].items():
            print(f"  {k:18} {v['covered_pct']}%  ({v['actions'] - v['missed']} of {v['actions']})")
        for key in ("FM2", "FM3"):
            a = res[key]["judge_vs_team"]
            print(f"{key} judge agrees with the team's notes on {a['agree']} of {a['of']} runs "
                  f"(disagrees: {', '.join(map(str, a['disagree_runs']))})")


# ---------------------------------------------------------------- one run

def _hide(secret: str) -> str:
    """'Passcode: 123456' -> 'Passcode: ••••', '?pwd=abc…' -> '?pwd=••••'."""
    return re.sub(r"([:=]\s*).*$", r"\1••••", secret) if re.search(r"[:=]", secret) else \
        re.sub(r"\S+$", "••••", secret)


def _mask(text: str, fm) -> str:
    text = EMAIL.sub("<email>", text)
    for p in fm.PASSCODE:
        text = p.sub(lambda m: _hide(m.group(0)), text)
    return text


def _excerpt(r: dict, fm, n: int = 10) -> list[str]:
    skip = ("From:", "To:", "Cc:", "--- Message", "Meeting note dated")
    lines = [ln.strip() for ln in r["input"].splitlines() if ln.strip() and not ln.strip().startswith(skip)]
    return [_mask(ln[:110] + ("…" if len(ln) > 110 else ""), fm) for ln in lines[:n]]


def _items(items: list[dict]) -> list[str]:
    return [f"{n}. {i.get('type')} · owner {i.get('owner_side') or '–'} — {i.get('text')}"
            for n, i in enumerate(items, 1)] or ["(nothing recorded)"]


def run(n: int, live: bool, use_judge: bool, only: set[str]) -> list[str]:
    from datetime import date

    from psycopg.rows import dict_row

    from app import db, notes
    from app.gmail import redact_secrets
    from app.ingest import _workspace_id
    from evals import failure_modes as fm

    runs = {r["run"]: r for r in fm.load_runs(SHEET)}
    r = runs[n]
    then = r["version"]
    v2 = json.loads(fm.V2_CACHE.read_text())[str(n)]
    if live:
        with db.connect() as conn:
            conn.row_factory = dict_row
            ws = _workspace_id(conn)
            team = conn.execute("select owner_code, full_name, email from team_members where workspace_id = %s",
                                (ws,)).fetchall()
            v2 = fm.v2_pipeline(conn, ws, team)(r)

    out = [f"RUN {n} · {r['source']} · tested on {then} ({VERSIONS[then]})", "",
           f"Team note — {r['judge']}, {r['verdict']}, failure mode: {r['failure_mode']}"]
    out += [f"  {r['note']}", "", "Input (excerpt):"] + [f"  {ln}" for ln in _excerpt(r, fm)]
    out += ["", f"{then} output ({VERSIONS[then]}):"] + [f"  {ln}" for ln in _items(r["items"])]
    out += ["", f"v2 output ({VERSIONS['v2']}, {v2.get('extractor', 'extract-v4')}"
                f"{', re-run live' if live else ''}): category {v2['category']}, deal {v2['company'] or '–'}"]
    out += [f"  {ln}" for ln in _items(v2["items"])]

    out += ["", "CHECKS"]
    show = lambda key: not only or key in only  # noqa: E731
    label = (n not in fm.NOT_SALES) if then == "v0" else r["relevant"]
    ok = lambda b: PASS if b else FAIL  # noqa: E731
    if show("FM1"):
        out.append(f"  FM1 Sales relevance [code vs team label]: label {'relevant' if label else 'not relevant'} · "
                   f"{then} {ok(r['relevant'] == label)} · v2 {ok(v2['relevant'] == label)}")

    judged = show("FM2") or show("FM3")
    if judged and use_judge and n not in fm.NOT_SALES and (r["relevant"] or v2["relevant"] or n in fm.MISSED_ACTIONS):
        client = fm.claude()
        a, b = fm.judge(client, r["input"], r["items"]), fm.judge(client, redact_secrets(r["input"]), v2["items"])
        for ver, j in ((then, a), ("v2", b)) if show("FM2") else ():
            out.append(f"  FM2 Missing actions [AI judge, live] {ver}: {j['todos']} Bianca action(s), "
                       f"{len(j['missed'])} missed → {ok(not j['missed'])}")
            out += [f"      missed: {m}" for m in j["missed"]]
        for ver, j in ((then, a), ("v2", b)) if show("FM3") else ():
            bad = j["wrong_type"] + [f"records {j['duplicate_quotes']} share one quote"] * bool(j["duplicate_quotes"])
            out.append(f"  FM3 Event type [code + AI judge] {ver}: {ok(not bad)}")
            out += [f"      {w}" for w in bad]
    elif judged and use_judge:
        out.append("  FM2/FM3 [AI judge]: not graded (the team labelled this item not sales-relevant)")

    sent_then, sent_now = r["input"], redact_secrets(r["input"])
    if show("FM4"):
        out.append(f"  FM4 Sensitive input [code]: text sent to the AI — {then} "
                   f"{ok(not fm.has_passcode(sent_then))} · v2 {ok(not fm.has_passcode(sent_now))}")
    if show("FM4") and fm.has_passcode(sent_then):
        hit = next(m for p in fm.PASSCODE for m in [p.search(sent_then)] if m)
        out.append(f"      {then} sent “{_hide(hit.group(0))}” (masked here); v2 sends “[redacted]” in its place "
                   f"({sent_now.count('[redacted]')} redaction(s) in this item)")

    if show("FM5") and r["source"] == "notes":
        today = date.today()
        body = "\n".join(ln for ln in r["input"].splitlines() if not ln.startswith("Meeting note dated"))
        secs = notes.split_sections(body, today)
        out.append(f"  FM5 Merged meetings [code]: {then} {ok(not fm.extra_headers(r['input'], today))} · "
                   f"v2 splitter → {len(secs)} meeting(s): {' | '.join(s.header for s in secs)} "
                   f"{ok(all(not fm.extra_headers(s.text, today) for s in secs))}")
    return out


# ---------------------------------------------------------------- product run

def run_log() -> None:
    from app import db
    with db.connect() as conn:
        r = conn.execute("select for_date, stage, steps, started_at, finished_at, log from daily_runs"
                         " order by for_date desc, started_at desc limit 1").fetchone()
    for_date, stage, steps, started, finished, log = r
    print(f"Processing run for {for_date}: {stage}, {steps} steps, started {started:%H:%M} UTC"
          f"{f', finished {finished:%H:%M} UTC' if finished else ''}\n")
    totals: dict[str, dict] = {}
    for e in log:
        t = totals.setdefault(e["stage"], {"calls": 0})
        t["calls"] += 1
        for k, v in e.items():
            if isinstance(v, int) and k not in ("remaining_threads", "remaining_units"):
                t[k] = t.get(k, 0) + v
    labels = {"classify": "1. Classify (AI): is it commercial, which company?",
              "resolve": "2. Link to companies (rules + AI's guess)",
              "extract": "3. Extract insights (AI), every quote checked against the source",
              "reconcile": "4. Completion check (AI): which open to-dos look done?",
              "profile": "5. Refresh deal profiles (AI)"}
    for stage_key, label in labels.items():
        t = totals.get(stage_key)
        if t:
            detail = ", ".join(f"{k} {v}" for k, v in t.items() if k not in ("calls", "stage"))
            print(f"{label}\n     {t['calls']} call(s): {detail}")


def items(n: int) -> None:
    from psycopg.rows import dict_row

    from app import db
    from app.extractor import _squash
    with db.connect() as conn:
        conn.row_factory = dict_row
        units = conn.execute(
            "select r.source, r.thread_id, max(r.updated_at) as at from source_records r where r.status = 'classified'"
            " and (r.classification->>'is_commercial')::bool and r.company_id is not null"
            " group by r.source, r.thread_id order by at desc limit %s", (n,)).fetchall()
        for u in units:
            recs = conn.execute(
                "select r.subject, r.occurred_at, r.created_at, r.participants, r.body, r.classification, r.link_method,"
                " r.extracted_version, r.meta, c.name as company from source_records r left join companies c"
                " on c.id = r.company_id where r.source = %s and r.thread_id = %s order by r.occurred_at",
                (u["source"], u["thread_id"])).fetchall()
            ins = conn.execute("select type, owner_side, due_date, text, quote, extractor_version from insights"
                               " where source = %s and unit_id = %s order by occurred_at, id",
                               (u["source"], u["thread_id"])).fetchall()
            last, c = recs[-1], recs[-1]["classification"] or {}
            senders = {a["address"].split("@")[-1] for x in recs for a in (x["participants"] or {}).get("from") or []}
            boxes = sorted({b for x in recs for b in (x["meta"] or {}).get("mailboxes") or []})
            print("=" * 100)
            print(f"{u['source'].upper()} · {(last['subject'] or '(no subject)')[:80]} · {last['occurred_at']:%b %d %H:%M} UTC")
            print(f"  Stored     {len(recs)} record(s), latest stored {last['created_at']:%b %d %H:%M} UTC"
                  f"{' · from ' + ', '.join(sorted(senders)) if senders else ''}"
                  f"{' · inbox ' + '+'.join(boxes) if boxes else ''} · secrets redacted before storage")
            print(f"  Classified [{c.get('prompt_version')}] {c.get('category')} · company {c.get('company')} · "
                  f"confidence {c.get('confidence')}")
            print(f"             why: {EMAIL.sub('<email>', c.get('reason') or '')[:160]}")
            print(f"  Linked     {last['company']} (by {last['link_method']})")
            text = _squash("\n".join(x["body"] or "" for x in recs))
            ok_quotes = sum(1 for i in ins if _squash(i["quote"]) in text)
            print(f"  Extracted  [{ins[0]['extractor_version'] if ins else last['extracted_version']}] {len(ins)} insight(s);"
                  f" quotes found verbatim in the source, re-checked now: {ok_quotes}/{len(ins)}")
            for i in ins:
                due = f" · due {i['due_date']}" if i["due_date"] else ""
                print(f"    - {i['type']} · owner {i['owner_side'] or '–'}{due}: {i['text']}")
                print(f"        “{EMAIL.sub('<email>', i['quote'])[:110]}”")


def brief() -> None:
    import subprocess

    import httpx

    from app.config import settings
    r = httpx.post("https://bianca-cro-api.onrender.com/brief/build", params={"version": "v2", "dry_run": "true"},
                   headers={"X-Ingest-Token": settings.ingest_token.get_secret_value()}, timeout=180)
    r.raise_for_status()
    d = r.json()
    out = REVIEW / "demo_runs" / f"brief_{d['for_date']}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(d["html"])
    print(f"Brief for {d['for_date']} (dry run: nothing stored, nothing sent)\n  Subject: {d['subject']}\n  Saved: {out}")
    subprocess.run(["open", str(out)], check=False)


def main(args: list[str]) -> None:
    if not args or args[0] == "summary":
        summary()
        return
    if args[0] == "run-log":
        run_log()
        return
    if args[0] == "item":
        items(int(args[1]) if len(args) > 1 else 1)
        return
    if args[0] == "brief":
        brief()
        return
    n = int(args[1])
    only = set(args[args.index("--only") + 1].upper().split(",")) if "--only" in args else set()
    out = run(n, "--live" in args, "--no-judge" not in args, only)
    print("\n".join(out))
    if "--save" in args:
        d = REVIEW / "demo_runs"
        d.mkdir(exist_ok=True)
        (d / f"run-{n:02d}.txt").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
