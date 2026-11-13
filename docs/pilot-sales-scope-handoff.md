# Pilot sales-scope handoff

## Review scope

Bianca's Daily Brief and War Room share extracted insights. For deals whose Pipeline `Status` is `Pilot` or its numbered form, such as `4. Pilot`, new source material needs to identify a specific Bianca commercial step or buyer decision before an insight qualifies as `deal_progression`. The matcher uses the status label and ignores case and surrounding whitespace.

| Source evidence | Expected handling |
| --- | --- |
| “Can you fix the dashboard by Friday?” | Keep the repair under `delivery`. |
| “We need the dashboard results to decide on Friday whether to proceed with a paid rollout.” | Capture the paid-rollout decision dependency as a sales blocker. Any repair remains delivery. |
| “Please add 20 more dashboard users.” | Keep the access request under `delivery`. |
| “Please send pricing for 20 additional seats.” | Capture a sales `next_step`. Preserve its status as a request. |

- [Status matching](../backend/app/pipeline.py) and the [extraction prompt](../backend/app/extractor.py) determine when the tighter instructions apply.
- [Source processing](../backend/app/process.py) identifies source records whose extraction version is blank before calling the model. In a mixed thread, only those records get the tighter Pilot rule. Earlier records are still passed as context and use the existing prompt rules on ordinary reprocessing.
- The [profile prompt](../backend/app/profiler.py) checks that any newly generated Pilot recommendation directly addresses a cited sales step. It uses the stored insight scope and status. The Daily Brief builders and War Room keep their existing eligibility logic, including blank legacy scope.
- The Daily Brief v2 `Waiting` classification is a separate path. Its fallback to a reply expectation without a matching extracted insight is outside this change.

## Verification

From a checkout with the locked backend test dependencies installed:

```sh
cd backend
uv run pytest -q
```

**582 passed**. The tests cover the model request for numbered and bare Pilot labels, non-Pilot labels, and batches of already extracted sources. They also cover a mixed sales and delivery message in both Brief versions and exercise both War Room suggestion columns. Prompt tests capture the request using a mock, while output tests use preclassified fixtures. Staging replay with the live model is the remaining semantic check.

## Production rollout

1. Deploy the PR branch to staging. Use an already configured staging War Room and a Pipeline row marked `4. Pilot`, plus a non-Pilot control. Ingest fresh fixture material with the paired examples above through the normal processing path. Include a Pilot thread with both previously processed and new messages.
2. Preview both Brief versions with authenticated `POST /brief/build?version=v1&dry_run=true` and `POST /brief/build?version=v2&dry_run=true`. These exercise output selection without sending. Where staging uses verification, also use `verification=shadow` with the current sync receipts as described in the [Daily Brief runbook](../README.md#daily-brief-verification-and-n8n-rollout). Check the War Room after staging's normal review build in `/review/app`. Compare Next Step and Client Context with the sources. Inspect suggestions without approving or writing to a production Sheet.
3. After staging sign-off, merge and deploy the new `master` revision through the normal backend release path. Keep existing feature flags, the n8n schedule and send gate, and Supabase configuration. This PR requires zero migrations, zero backfills, and no forced queue rebuild. Leave `REVIEW_ENABLED` at its current setting.
4. Confirm the first normal production cycle processes fresh Pilot data successfully and check the next no-send Brief preview. Where the War Room is already enabled, inspect its next regular queue for fresh Pilot suggestions and a non-Pilot control. If the cycle has no new Pilot material, record that production coverage is pending until one arrives.
5. If the change suppresses valid sales work or causes processing failures, revert the PR and redeploy the previous backend. This restores the previous prompts for future processing. It does not undo classifications already persisted, Briefs already delivered, or Sheet edits already approved. Review affected items individually before any targeted correction.
