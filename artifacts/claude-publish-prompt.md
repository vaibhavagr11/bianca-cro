# Publish in the founders' Claude Enterprise account

Use this prompt in the actual account after the hosted backend and Bianca Review connector are configured. Upload `sales-pipeline-updates.html` or make it accessible to the Claude session.

> Publish the attached HTML as a private, connector-backed artifact titled **Sales Pipeline Updates** for the Bianca founders. Preserve its approved layout, blue/navy styling, owner options RG / NC / NDL, company cards, bullet editing, and Approve / Edit / Ignore actions.
>
> Keep only the **To review** and **Activity** tabs. Show each source as its type and date (for example, **Notes · Sep 28** or **Email · Oct 6**) without quotations. Preserve the **Current Next steps** dropdown on every company card, including multiline cell contents and the empty state. Do not add a Sheet preview tab or the removed cell-movement footer text.
>
> Preserve the separate **Next Step** and **Client Context** bullet sections and editors, and the **Current Client Context** dropdown. Next steps, commitments, and blockers belong to Next Step; customer needs, buying signals, and objections belong to Client Context. Approval arguments must include both `next_step_bullets` and `client_context_bullets` arrays. Never flatten them into the legacy `bullets` argument. Empty sections leave existing cells unchanged; context is added to the existing Client Context without repeating equivalent bullet lines.
>
> Use the **Bianca Review** connector. Discover and validate its real tool names before publishing: `bianca_review_workspace`, `bianca_approve_update`, and `bianca_ignore_update`. Declare these connector capabilities in the artifact. Use only the viewing user's authorized connection.
>
> The HTML expects `window.biancaConnector.callTool(name, args)`. Implement that small adapter using the actual native artifact connector API available in this Claude account; do not invent an API signature. Install it before the page scripts initialize, or dispatch `bianca:connector-ready` afterward. Return plain JSON or standard MCP tool-result envelopes. Return failed operations as `isError: true` and include a safe `detail` string and HTTP-style numeric `status`.
>
> Keep the server as the source of truth. Do not replace the live queue with sample data, store company data in browser storage, put tokens into this HTML, or directly fetch an external backend from inside the artifact. If tools are missing or the connector is unavailable, show the existing connection-required state and stop the integration rather than simulating success.
>
> Verify reading the pending queue first. For the initial write test, use a designated non-production test company and a disposable test Google Sheet that the owner explicitly identifies. Configure that test Sheet's ID, tab, and numeric sheet ID explicitly. Confirm that action approval places the exact former Next step in Last progress and writes approved action bullets into Next step. Context approval adds the reviewed context bullets to Client Context. Test action-only, context-only, and mixed approval; context-only must leave Next step and Last progress unchanged. Other rows/columns must be untouched. Verify that Edit applies both edited sections, Ignore leaves all three cells unchanged, NDL filtering works, and repeated requests cannot rotate or append twice.
>
> Share only with the intended Bianca founders inside the Enterprise organization after the connector and artifact permissions are confirmed. Do not publish this workspace publicly.

Official capability references:

- [Artifacts and connector behavior](https://code.claude.com/docs/en/artifacts#pull-live-data-with-mcp-connectors)
- [Custom remote connector setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)

This prompt configures the account-specific bridge. Committing the HTML to GitHub alone does not create or authorize an artifact in Claude Enterprise.
