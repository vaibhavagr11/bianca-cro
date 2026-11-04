# Sales Pipeline Updates

The approved interface is delivered in two self-contained pages:

- `sales-pipeline-updates.html` is the **live** client. It starts empty and requires a configured Bianca Review connector or a signed-in session on the hosted Bianca backend.
- `sales-pipeline-updates-demo.html` is the **fictional-data preview**. It keeps demo decisions in browser storage and never contacts Google Sheets or the backend.

Both use the approved blue/navy design, the title **Sales Pipeline Updates**, and owner filters for Ramiro (`RG`), Nico Casaux (`NC`), and Nico de León (`NDL`). The demo assigns Farmater to NDL to demonstrate that filter.

## Hosted backend

Open `/review/app` on the Bianca backend. Enter your assigned reviewer access token in the connection panel. It is held only in this page's memory, is sent in the Authorization header, and is cleared on disconnect or page exit. The live page does not put credentials or company data in localStorage, sessionStorage, a URL, or the HTML source. The page requests only fixed, same-origin `/review/...` routes.

Each card separates proposed **Next Step** bullets from proposed **Client Context** bullets. The **Current Next steps** and **Current Client Context** dropdowns show the company's values from the backend's latest synced Pipeline row, falling back to the proposal snapshot if that row is unavailable. They preserve wording and line breaks and show explicit empty states. Approval re-reads the actual Google Sheet before writing; synced context may lag a collaborator's latest edit.

## Claude Enterprise artifact

Use `claude-publish-prompt.md` after an Enterprise administrator has added the Bianca Review remote connector and the founders have connected it. The artifact's runtime must declare access to these tools:

| Tool | Arguments |
| --- | --- |
| `bianca_review_workspace` | `{}` |
| `bianca_approve_update` | `{proposal_id, revision, idempotency_key, next_step_bullets, client_context_bullets}` |
| `bianca_ignore_update` | `{proposal_id, revision, idempotency_key}` |

The host-specific JavaScript bridge is generated and validated inside the actual Claude account during publishing. This file does not guess an undocumented Claude API. The integration installs this narrow adapter before the page runs:

```js
window.biancaConnector = {
  async callTool(name, args) {
    // Invoke the declared connector tool using the actual Claude artifact runtime.
    // Return the tool result without embedding credentials in this page.
  }
};
```

If the host installs it after page initialization, it dispatches:

```js
window.dispatchEvent(new Event('bianca:connector-ready'));
```

`callTool` may return a plain JSON object or an MCP tool result with `structuredContent` or a JSON text `content` block. A failed tool call must set `isError: true`, with `{detail: string, status: number}` in its structured or JSON text result.

The artifact should never make a direct request to an external Render endpoint: the declared Claude connector performs the server call. If a viewer has not connected Bianca Review, the live page displays an explicit connection message and shows no fabricated data.

## Review behavior

- The two views are **To review** and **Activity**. Sheet context is available inside each card rather than a separate Sheet preview tab.
- Sources appear as type-and-date labels, such as **Notes · Sep 28**, **Email · Oct 6**, and **Calendar · Oct 8**. Source quotations are not displayed.
- Expand **Current Next steps** or **Current Client Context** to read the existing Sheet values before reviewing the proposed bullets.
- **Next Step** receives next steps, commitments, and blockers. **Client Context** receives customer needs, buying signals, and objections. Categories are routed from stored insight types, never inferred from editable text prefixes.
- Approve sends both explicit bullet lists and the expected proposal revision to the server. The card disappears only after a confirmed server decision and a refreshed shared workspace.
- Edit opens separate Next Step and Client Context editors and shows the existing values. **Save & approve** submits both lists. Either list may be empty, but an approval must contain at least one bullet overall.
- Ignore records a shared dismissal without writing the sheet. Undo is available in the demo only.
- When action bullets are approved, Last progress is replaced by the exact previous Next step and the new actions replace Next step. If no action bullets are approved, both cells stay unchanged.
- Approved context bullets are added to existing Client Context without repeating equivalent bullet lines. If no context bullets are approved, that cell stays unchanged. Existing context is not rotated into Last progress.
- Network errors keep the card visible. An unresolved approval is displayed as **Approval being confirmed**, including the backend’s specific recovery or operator-reconciliation message, with a retry of the original durable request. The retry cannot submit new wording or rotate the sheet a second time.
- If the Google Sheet is updated but its backend mirror is not refreshed, the page explicitly shows **Pipeline snapshot needs a full sync**. The notice remains until the snapshot matches the approved after-values or `rows.lastSyncedAt` confirms a later full Pipeline sync. Recent activity carries `mirror_refreshed` so an unresolved notice is restored when reopening the workspace; only the latest approval per company is considered.
- Refresh retrieves shared decisions from other founders. There is no four-company brief limit on the review queue.

The default Sheet is `14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw`, tab `Pipeline`, sheet ID `1869050409`. Deployment variables override defaults and must be checked before enabling writes. Apply migration 013, sync the full tab including Client Context, and rebuild pending proposals before publishing this client. The old flat `bullets` approval format is rejected. Historical applying approvals without typed routing require operator reconciliation; they are never automatically retargeted to the new Sheet. Every new durable approval binds its spreadsheet, tab title, and sheet ID.

## Frontend verification

Run with Node.js:

```sh
node tests-frontend/verify-live.cjs
node tests-frontend/verify-demo.cjs
```

These checks exercise the actual scripts embedded in the HTML, including connector/REST transport, fail-closed startup, credentials handling, delayed and failed writes, shared-state refresh, revision conflicts, stable retry keys, and applying-request recovery. Browser appearance and the account-specific Claude connector bridge still need verification in the deployment environment.
