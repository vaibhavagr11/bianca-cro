"""Write the latest profiles of the given deals for human review (contains Bianca data: never commit).

Outputs, in <repo>/review/:
  profiles.md     readable profiles with evidence quotes
  validation.csv  one row per statement; reviewers fill 'verdict' (correct / partly / wrong) and 'notes',
                  and add rows under section 'missing' for anything the profile left out

Usage (from backend/): uv run python -m scripts.export_review "FEMSA Arg" "Koala Eco" ...
"""

import csv
import sys
from pathlib import Path

from psycopg.rows import dict_row

from app import db

REVIEW_DIR = Path(__file__).resolve().parents[2] / "review"
SECTIONS = ["needs", "buying_signals", "objections", "blockers", "open_commitments", "recommended_actions"]


def main(companies: list[str]) -> None:
    REVIEW_DIR.mkdir(exist_ok=True)
    md: list[str] = ["# Deal profiles for review", ""]
    rows: list[dict] = []
    with db.connect() as conn:
        conn.row_factory = dict_row
        for company in companies:
            p = conn.execute(
                "select op.profile, op.generated_at, o.stage from opportunity_profiles op"
                " join opportunities o on o.id = op.opportunity_id join companies c on c.id = o.company_id"
                " where c.name = %s order by op.generated_at desc limit 1", (company,)).fetchone()
            if p is None:
                print(f"no profile for {company}")
                continue
            prof = p["profile"]
            ids = list(prof.get("refs", {}).values())
            insights = {str(r["id"]): r for r in conn.execute(
                "select id, type, quote, source, occurred_at from insights where id::text = any(%s)", (ids,))}

            def evidence(refs: list[str]) -> str:
                out = []
                for ref in refs:
                    i = insights.get(prof["refs"].get(ref, ""))
                    if i:
                        out.append(f"[{ref} {str(i['occurred_at'])[:10]} {i['source']}] \"{i['quote']}\"")
                return "\n".join(out)

            md += [f"## {company} — {p['stage']}", "",
                   f"**Summary** ({prof['summary']['basis']}): {prof['summary']['text']}", "",
                   f"**Momentum:** {prof['momentum']} — {prof['momentum_reason']['text']}", ""]
            rows.append({"deal": company, "section": "summary", "statement": prof["summary"]["text"],
                         "basis": prof["summary"]["basis"], "evidence": evidence(prof["summary"]["evidence"])})
            rows.append({"deal": company, "section": "momentum",
                         "statement": f"{prof['momentum']}: {prof['momentum_reason']['text']}",
                         "basis": prof["momentum_reason"]["basis"],
                         "evidence": evidence(prof["momentum_reason"]["evidence"])})

            md.append("**Stakeholders:** " + "; ".join(
                f"{s['name']} ({s['side']}{', ' + s['role'] if s.get('role') else ''})" for s in prof["stakeholders"]))
            md.append("")
            for s in prof["stakeholders"]:
                rows.append({"deal": company, "section": "stakeholders",
                             "statement": f"{s['name']} — {s['side']}{', ' + s['role'] if s.get('role') else ''}",
                             "basis": "", "evidence": evidence(s["evidence"])})

            for section in SECTIONS:
                items = prof.get(section) or []
                md.append(f"**{section.replace('_', ' ').title()}**")
                if not items:
                    md.append("- (none)")
                for it in items:
                    if section == "open_commitments":
                        text = f"{it['text']} — owner: {it.get('owner') or '?'}, due: {it.get('due') or '-'}, {it['state']}"
                        basis = it["state"]
                    elif section == "recommended_actions":
                        text = f"[{it['urgency']}] {it['action']} — why: {it['why']}"
                        basis = "inferred"
                    else:
                        text, basis = it["text"], it["basis"]
                    md.append(f"- {text} _({basis}; {', '.join(it['evidence'])})_")
                    rows.append({"deal": company, "section": section, "statement": text, "basis": basis,
                                 "evidence": evidence(it["evidence"])})
                md.append("")
            md.append("**Unknowns:** " + ("; ".join(prof.get("unknowns") or []) or "(none)"))
            md.append("")
            for u in prof.get("unknowns") or []:
                rows.append({"deal": company, "section": "unknowns", "statement": u, "basis": "", "evidence": ""})
            rows.append({"deal": company, "section": "missing", "statement": "(add anything important the profile "
                         "missed, one row each)", "basis": "", "evidence": ""})

    (REVIEW_DIR / "profiles.md").write_text("\n".join(md), encoding="utf-8")
    with open(REVIEW_DIR / "validation.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["deal", "section", "statement", "basis", "evidence", "verdict", "notes"])
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "verdict": "", "notes": ""})
    print(f"wrote {REVIEW_DIR / 'profiles.md'} and {REVIEW_DIR / 'validation.csv'} ({len(rows)} rows)")


if __name__ == "__main__":
    main(sys.argv[1:])
