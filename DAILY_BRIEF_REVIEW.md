# Daily Brief 160-character limit review handoff

[PR #3](https://github.com/vaibhavagr11/bianca-cro/pull/3) changes how Bianca's Daily Brief displays long table descriptions. This handoff covers the backend change and the checks that require access to the deployed backend, n8n, Supabase, or the Anthropic workspace. The scheduled workflow, production rows, and live model responses were outside the scope of the local verification.

## What the backend does

1. **Builds and selects the rows.** The authenticated `POST /brief/build` endpoint accepts `version` and `dry_run` as query parameters. It defaults to `v1` and a non-dry build. `version=v2` selects the deal-grouped format with **Situation** and **Needs you today**. Both versions receive the change. The backend reads the workspace's source records, extracted insights, opportunity profiles, and action state from Supabase Postgres. It assembles and selects the rows before preparing display text. The existing planner can still word recommendations and meeting prep. [Build and response code](backend/app/brief_api.py) · [Database connection](backend/app/db.py)
2. **Keeps the source wording available.** In v2, a verified blocker or profile summary supplies **Situation**, and an extracted insight or reply instruction supplies **What**. The covered table paths use their full assembled wording before display preparation. The change removes the old 95-character table formatter and earlier shortening of extracted reply requests and some change descriptions. A subject-only record is labeled for the model so it can describe the topic without inferring a request or outcome. V1 also passes its full detail through the existing planner fallback. [V2 row selection and wording](backend/app/brief_v2.py) · [V1 wording](backend/app/brief.py)
3. **Shortens only fields that exceed the limit.** `prepare_table_text()` collects the descriptions for rows that will render. Text at or below 160 Python characters passes through as supplied. Longer fields are deduplicated by their text, type, and validation requirements, then sent to Claude in one structured request with per-field IDs. The request uses the existing `ANTHROPIC_MODEL` and client, an SDK timeout setting of 10 seconds, and zero SDK retries. It includes at most 32 fields and 24,000 serialized input characters, with a 4,096-token output limit. An all-short brief skips this extra request. The regular brief planner can still call Claude. [Display preparation](backend/app/brief_text.py) · [Claude configuration](backend/app/config.py)
4. **Validates and renders.** A usable rewrite has a matching ID, fits within 160 characters, and passes checks for blank output, clipped prefixes, ellipses, prose punctuation, and explicitly required phrases where supplied. A missing or rejected rewrite may use an explicit, validated fallback. The waiting-status fallback can preserve reply status while leaving out a long recipient label. If neither path yields usable wording (for example, Claude is unavailable), the text is cut deterministically: the first complete sentence that fits, otherwise whole words with an ellipsis, keeping any required phrase. The brief always builds and sends. [Validation and fallback](backend/app/brief_text.py) · [Error and archive paths](backend/app/brief_api.py)

### Display and archive scope

| Area | What the branch does |
| --- | --- |
| V2 HTML | Caps descriptions for both **What** tables, **Situation**, both **Next move** columns, **Who · status**, meeting title and **Prep**, and **What changed**. |
| V1 HTML | Caps action and explanation together where they form one narrative, plus meeting title, **Prep**, and **What changed**. |
| Plain text | Uses the same prepared wording for fields it already includes. V2 plain text still omits **Situation**, the shared urgent table, and **What changed**. Confirm which alternative the mail node uses. |
| Character counting | Counts the description before HTML escaping, including its spaces and punctuation. Deal names, badges, separate date and source labels, and links are metadata outside this budget. A whole visual cell can therefore contain more than 160 characters. |
| Other content | V2 follow-up paragraphs still use their separate 95-character formatter. The existing **Done** confirmation can show the first 120 characters of a label that differs from the rewritten cell. Its URL contains that label in a signed, readable token. Display rewrites never supply completion IDs. |
| Supabase archive | Successful non-dry builds store `html` and the structured `payload` separately in `briefs`. The payload uses the assembled rows before display rewriting. In particular, `payload.sections[].highlights[].situation` can now hold the full verified note where it previously held an excerpt. A payload consumer that displays its text directly needs its own formatting or should use the returned HTML. |

[V2 renderers and payload](backend/app/brief_v2.py) · [V1 and shared meeting renderers](backend/app/brief.py) · [Done link handling](backend/app/actions.py) · [Archive schema](backend/migrations/007_briefs.sql)

## Review before merge

### n8n and deployed backend

- **Confirm the exact scheduled path.** Find the enabled n8n workflow, its target URL and deployed Git revision, request parameters, and the mail node. Verify the active workflow passes the intended `version`, uses the returned `html` and `text` as appropriate, and takes `subject` and recipients from the intended source. Check for a separate formatter, a cached body, or a second sender. For a v2 staging preview of this PR, use authenticated `POST /brief/build?version=v2&dry_run=true` with the staging ingest token. Exercise v1 as well if scheduled. Keep the mail node disconnected or use an approved test recipient. Dry run suppresses the archive write and still reads data, can call Claude, and returns email content and recipients.
- **Exercise the fallback.** Force the shortening call to fail in staging and confirm the brief still sends, with over-limit rows cut at a word boundary and required phrases kept.
- **Check latency and retry policy.** Time a realistic all-short run and a run with several long fields on the configured model. The extra Claude call runs after row selection and the planner, so the whole build can exceed the extra call's SDK timeout setting. Check n8n and hosting timeouts, Anthropic permissions and rate limits, and retries. A retry can rerun model calls. Successful builds archive before n8n sends, and the latest archive timestamp feeds the next brief's change window. Rebuilding after a successful build or retrying after an uncertain send can create extra archive rows or duplicate delivery. Verify the workflow's deduplication and recovery policy separately from the wording change.
- **Confirm the fallback policy with the developer.** One unsummarizable field without an approved fallback can delay the entire brief. The v2 urgent list can exceed the 32-field model budget. Any over-limit description outside that budget, or individually too large for the input limit, needs a fallback or review. Retrying unchanged inputs cannot fix that case. If this is too brittle for the morning send, choose between bounded additional batches and grounded field-specific fallbacks, and make the review reason actionable. Retain the 160-character validation on every rendering path for these descriptions.

### Supabase data and model behavior

- **Check representative rows against their evidence.** In an authorized staging environment, use anonymized or approved examples from a verified blocker, a multi-sentence deal summary, a dated commitment, a reply with an extracted request, a reply with only a subject, a meeting, and a recorded change. Compare the table text with the source insight/profile and the original evidence. Look for changes in who owes what, the date or amount, the distinction between proposed and confirmed, and whether a blocker is still open. Check that the scheduled build follows successful ingestion and processing for the intended workspace. A short rewrite cannot correct missing or stale source data.
- **Review the semantic checks explicitly.** `_valid_shortening()` can accept a plausible rewrite that reverses a negation, changes a deadline, swaps the text for two valid IDs, or preserves a first sentence while dropping a later correction. It can even accept a fragment such as “Wait for.” if punctuation and length pass. Most fields rely on Claude's instructions for factual fidelity. Short inputs also pass through with any existing fragments or ellipses. Test adversarial examples before enabling live delivery. Consider preserving structured facts such as owner and deadline as separate, authoritative fields and routing material uncertainty to review.
- **Inspect archive consumers and retention.** This branch adds no schema migration, Supabase function, or trigger. Check whether n8n, dashboards, exports, or other consumers read `briefs.payload` or the v2 response payload, and whether they assume **Situation** or changes are pre-shortened. Check the intended visibility and retention of the now-longer payload text and distinguish `briefs.html` from proof that the email was delivered.
- **Check the data sent to Claude.** The new request can include full assembled insight or profile wording, email subjects, meeting titles, prep text, and waiting-on-reply labels that may contain an address. Confirm the Anthropic workspace, data handling policy, and n8n execution/log retention. The prompt treats the field text as data, but instruction-like source content can still influence the rewrite. In both builders this PR also removes the old 300-character bound on the profile summary embedded in the separate meeting planner context. That context is not a table cell. Review whether to restore the planner-only bound, which can reduce the data sent even on days with no display rewrite. Keep real records, customer prompts, credentials, and sensitive screenshots out of this repository and the PR discussion.

### Related changes

[PR #2](https://github.com/vaibhavagr11/bianca-cro/pull/2) adds a pre-send verification flow. A local three-way check of the two PRs found overlapping edits requiring reconciliation in `brief.py`, `brief_api.py`, and `brief_v2.py`. This matters regardless of merge order. In the combined code, run display preparation after verification selects the final visible rows and before producing sendable HTML or plain text. PR #2's verified path bypasses the legacy planner and leaves its client unset. Wire the display model explicitly without re-enabling that planner. Keep verified action data separate from display wording, retain the sync receipts and `build_id` polling contract, and ensure only the approved ready path can send. Review whether a display failure maps to a blocked build or to a separate error. Test both replay and delivery deduplication with `build_id`.

## Test evidence and suggested sign-off

The backend suite passed **277 tests** on the implementation. New tests cover the 160/161-character boundary, selected v1 and v2 table fields, full-source batching, HTML escaping, consistent wording in fields shared by HTML and plain text, malformed model responses, and the review response. The tests stub Claude and use local test configuration.

```bash
cd backend
uv run pytest -q
```

Before approving, record the deployed revision and n8n path reviewed, the result of one staging render with approved long-field examples, the forced review outcome, and the policy for a blocked build. A successful sign-off includes a complete, faithful description at or below 160 characters, a valid destination and message body on success, and a visible operator outcome on review without a duplicate or stale send.

[Core tests](backend/tests/test_brief_text.py) · [V1 display tests](backend/tests/test_brief_v1_text.py) · [V2 render and API tests](backend/tests/test_brief_v2_text.py)


## 11/17/2026 - fixes from the 11/09 evals and the founders' 11/12 session

Branch `brief-fixes-1117`. Everything below is deterministic code in the v2 brief (the version n8n requests); no prompt, migration, n8n workflow or required setting changed. All new settings have defaults in [config.py](backend/app/config.py).

| What a founder sees | Rule | Where |
| --- | --- | --- |
| A date set in the Sheet brings the deal back | A Next step bullet ending in `(complete by Nov 16, 2026)` shows in **Needs you today** on that date, tagged **Sheet date**, source "Sheet next step". It stays, marked late, for `BRIEF_SHEET_STEP_OVERDUE_DAYS` (14), then becomes a one-line reminder to update the Sheet. Not for Paused deals or working clients. If the same step also exists as an extracted item, the item is held until the date and the Sheet row replaces it, so a step is never listed twice. Sheet rows have a Done button and are not sent to the pre-send check. | [brief_sheet_steps.py](backend/app/brief_sheet_steps.py), `brief_api._apply_sheet_dates`, `brief_v2.sheet_rows` |
| No "keep following up" rows | Nudge-only items and suggestions are replaced by one time-based reminder per deal: "3 weeks since your last email - follow up this week", on the first brief on or after each `BRIEF_NUDGE_EVERY_DAYS` (7) mark, on the Waiting row when there is one, else under Follow-ups. After `BRIEF_PAUSE_AFTER_EMAILS` (5) of our emails in a row with no reply: "5 emails with no reply - consider moving to Paused". | `brief_v2.Cadence`, `_split_followups`, `_waiting_status` |
| Waiting items never in Deals to move | A step or blocker that only says the customer has not answered ("Wait for Franco's reply", "No reply since Oct 21") is the waiting state, never a next move or a Situation. | `brief_followups.is_waiting_only` |
| Shorter tables | No table lists more than `BRIEF_MAX_TABLE_ROWS` (7) rows; a line under the table counts what was left out. A deal already in Needs you today gets no second, generic row in Deals to move. | `brief_v2.cap_tables`, `select_changes` |
| Quiet In Talks deals, a few at a time | "Quiet In Talks deals to revisit (3 of N)": In Talks, no recorded contact in over two weeks or none at all, nothing else open. `BRIEF_RESURFACE_PER_DAY` (3, 0 = off) per founder, longest-untouched first; a deal that was listed waits `BRIEF_RESURFACE_COOLDOWN_DAYS` (14), read from the archived briefs. | `brief_v2.build_section` |
| Pilots: sales items only | On a Pilot deal, items stored before scopes existed (blank scope) are hidden (`BRIEF_PILOT_STRICT_SCOPE`, default on). | `brief_api._scoped_rows` |
| Prep on every meeting | Written last, from rows that survived the pre-send check: who is on the invite and whether they accepted, then the most pressing row the brief shows for the deal, else its open issue, Sheet next step, summary or last contact. A meeting the classifier did not link is tied to a deal when a guest's address or domain points to exactly one pipeline company. | `brief_v2.meeting_prep`, `brief_api._deal_for_guests` |
| Cancelled meetings | Google's "Canceled event" mail, which is never stored, now marks the stored meeting cancelled at Gmail ingest when it is newer than the latest calendar sync. | `gmail.cancellation_notices`, `ingest.apply_cancellation_notices` |
| Situation text | Sentences no longer end at "2. In Talks", decimals, abbreviations, initials or inside a quote; a long Situation is cut at a clause break, never with an ellipsis. | `brief_text.split_sentences` |
| "Customer replied" | Help-desk ticket notices are system mail. "Customer replied" is said only for a sender on the deal company's own domain; anyone else is named by domain. Up to 7 changes show, customer-written ones first. | `brief_v2.is_automated`, `reply_origin` |
| Already-answered rows | Without the pre-send check, a row whose email thread we have written in since is marked "you wrote back Nov 5 - still open?" (not hidden: a later email can be a thank-you). | `brief_api.answered_after` |
| Stable wording | A regenerated suggestion that cites the same insights and says the same thing keeps its previous wording, so rows and Done clicks stay stable. | `profiler.keep_wording` |

### Check after deploy

- **Sheet dates.** In a dry run (`POST /brief/build?version=v2&dry_run=true`), confirm a row dated today in the live Sheet appears as **Sheet date** and that `payload.sections[].sheet_later` lists the future ones. A slash date such as 03/04/2026 is not read (ambiguous); the Hub writes month names.
- **Weekly nudge and quiet-deal batch.** Both read the `briefs` archive (`for_date`, `payload.sections[].resurfaced`). Dry runs do not archive, so a preview repeats the same nudge and batch until a real brief is sent.
- **Cancelled meetings.** Only works if the n8n Gmail search returns "Canceled event" mail; check `meetings_cancelled` in the `/ingest/gmail` response. Re-fetching the calendar right before the build is still the complete fix and is an n8n change.
- **Friday mail gap (finding 1).** Not fixed here: the Gmail search in n8n needs `newer_than:4d` instead of `newer_than:2d`. The backend already skips mail it has (`store_gmail` matches on Gmail id and Message-ID), so the longer window is safe.
- **Pilots.** Product-work items that were extracted with a sales scope before PR #17 still show; they need a re-extraction run on Pilot deals.

### Release review (11/17) - what changed after running the branch on a real database

Found by building eight consecutive days of briefs against Postgres from a seeded pipeline (36 deals), first with master's code and then with this branch on the same archive. Fixed here:

- **Nothing optional can stop the send.** Sheet dates, the weekly nudge, the quiet-deal batch, guest matching, meeting prep and the "wrote back" cue are extras: if one fails, the reason is logged (`brief built without ...`, `sheet dates skipped for opportunity ...`, `brief section RG rebuilt without ...`) and the brief is built without it. The archive reads run in a savepoint. At Gmail ingest a cancellation notice the database cannot read is skipped on its own; it used to fail the whole mail sync. A profile is still saved when its earlier wording cannot be carried over.
- **Blank or huge text.** A blank insight, action or summary no longer raises in the sentence splitter (it did since 11/17; before that it gave blank text). A Next step line with a long run of spaces after "(complete by" no longer stalls the build, and a very long text is read once.
- **Paused deals get no reminder.** No "follow up this week", no "consider moving to Paused" (the deal is already Paused) and no Follow-ups row. Their Waiting row reads as it did before 11/17.
- **The hold-back rule never hides what is due.** An extracted item that is due (or due tomorrow) or overdue by its own date stays in the brief even when the same step carries a later date in the Sheet.
- **Ticket numbers.** "Request #123" in a subject marks a system notice only when the sender is a help-desk host or a support mailbox. A buyer's "Solicitud N° 4521 - alta de proveedor" and our own replies in that thread are people again (they had dropped out of Reply needed and Waiting). A desk label in brackets ("[Acme Help Center]") is still enough on its own.
- Smaller: a deal with a meeting today or tomorrow is not listed as quiet; a long-past Sheet date from another year shows its year; "Nov 16, 26" is read as 2026.

Off switches (all default to the 11/17 behaviour; set the variable on the backend and the next build uses it):

| Behaviour | Turn off with | With it off |
| --- | --- | --- |
| 7-row cap | `BRIEF_MAX_TABLE_ROWS=1000` | Needs you today and Meetings list every row; Deals to move (6), Waiting (6) and Follow-ups (3) keep their earlier limits. What changed lists every change (the old 3 + 2 quota is gone for good). |
| Weekly nudge and "consider Paused" | `BRIEF_FOLLOWUP_NUDGES=false` | Reminders print as written and Waiting rows carry no nudge, as before 11/17. Waiting statements ("Wait for Franco's reply") are ordinary steps again. |
| Quiet In Talks batch | `BRIEF_RESURFACE_PER_DAY=0` | No batch; the "Confirm status" and "no record in 45+ days" lines count those deals as before. |
| Pilot strict scope | `BRIEF_PILOT_STRICT_SCOPE=false` | Not-yet-scoped items show on Pilot deals again. |
| Sheet dates | `BRIEF_SHEET_DATES=false` | "(complete by ...)" notes are not read. |
| Holding the matching item until the Sheet date | `BRIEF_SHEET_HOLD_MATCHING=false` | Sheet dates still show on their day; the extracted item keeps showing by its own rules before it. |

No switch (by design, they are corrections): prep lines and guest matching, cancelled meetings, "Customer replied" wording, Situation cuts, the "you wrote back" cue, kept wording, and "no second generic row for a deal already in Needs you today".

Known limits, for the team to decide: see the review report (Needs you today is capped at 7 with only a count for the rest; a Sheet-date row stays up to 14 days and sorts first; "consider moving to Paused" repeats daily; a missed brief day can bring two nudges less than 7 days apart; a cancellation notice matches by title and day only).

Tests: `cd backend && uv run pytest -q` (832 passed, 13 skipped, which need a local test database). The review's tests are in [test_brief_review_1117.py](backend/tests/test_brief_review_1117.py). The end-to-end fixture is [test_brief_fixture_1117.py](backend/tests/test_brief_fixture_1117.py); run it with `BRIEF_FIXTURE_HTML=<path>` to save the rendered email.
