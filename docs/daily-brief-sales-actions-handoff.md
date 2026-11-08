# Daily Brief sales actions handoff

Review this change before rolling it out to Bianca’s scheduled Daily Brief. Both brief versions now require source-backed evidence that an action advances Bianca’s own commercial relationship. A proposal, pilot decision, renewal, or expansion can qualify. Routine delivery and the customer’s own sales operations stay out of the action lists.

## Code to review

- The extractor classifies each insight as `deal_progression`, `delivery`, or `out_of_scope` and records the exact source. Its prompt asks for each distinct action, its supported owner and timing, and explicit agreement before using `commitment`. Timing windows stay in the text. [Extractor](../backend/app/extractor.py) · [Migration](../backend/migrations/012_action_scope.sql)
- Both brief builders use the same scope filter. Email rows require an eligible ask in the latest relevant message body. Recommendations require their own sales scope and current, qualifying evidence. A dated request can still be shown as a request. [Brief builders](../backend/app/brief_api.py) · [v2 action rules](../backend/app/brief_v2.py) · [Profiles](../backend/app/profiler.py)
- Backfill reviews eligible legacy units with an open action or Gmail activity in the last 90 days. It preserves IDs and completion state for matched insights. Legacy rows with unresolved scope and recommendations without current qualifying evidence stay out of the brief. A short email reminder that omits the actual sales ask can also be skipped. [Backfill](../backend/app/process.py)

## Verification

- **254 backend tests passed** on the published implementation tree. The regression tests exercise both brief versions with mixed sales and support work, two distinct sales commitments, dated requests, quoted email history, and sales and delivery recommendations. [Brief regression tests](../backend/tests/test_brief_sales_scope.py)
- A disposable PostgreSQL smoke test exercised the migration and backfill queries, confirmed that a matched action retained its ID and completion state, and checked repeat execution. Production validation is pending.

## Production rollout

1. Confirm which brief version the live n8n workflow requests. Pause scheduled sends and coordinate the backfill with the normal daily processing run.
2. From `backend/`, run `uv run python -m scripts.migrate` against the production database. Confirm `012_action_scope` is applied before the updated backend starts. Coordinate this step before merging if deployment runs automatically.
3. Deploy the updated backend. Get active opportunity IDs from authenticated `GET /opportunities`. Call `POST /process/extract` with `{"opportunity_ids":["<id>"],"backfill_scope":true}` for the active IDs. Repeat until `remaining_units` reaches zero. Resolve failed batches and collect the union of `touched_opportunity_ids` across successful batches.
4. Call `POST /process/profile` in batches with `{"opportunity_ids":["<touched-id>"]}`. Check the result for failed profiles and resolve failures before validating the brief.
5. Run authenticated `POST /brief/build?version=v1&dry_run=true` and `POST /brief/build?version=v2&dry_run=true`. Check that genuine sales actions survive, support work stays out, and dated requests retain their request label. Review the output of the live n8n version before resuming sends.

If rollout validation fails, keep sends paused. Check extraction-version compatibility before restoring an older backend. The current `master` extractor predates `extract-v5` and can reprocess rows that the new backend already classified.
