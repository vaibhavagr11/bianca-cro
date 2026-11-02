# Publish in the founders' Claude Enterprise account

Use this prompt in the actual account after the hosted backend and Bianca Review connector are configured. Upload `sales-pipeline-updates.html` or make it accessible to the Claude session.

> Publish the attached HTML as a private, connector-backed artifact titled **Sales Pipeline Updates** for the Bianca founders. Preserve its approved layout, blue/navy styling, owner options RG / NC / NDL, company cards, bullet editing, and Approve / Edit / Ignore actions.
>
> Use the **Bianca Review** connector. Discover and validate its real tool names before publishing: `bianca_review_workspace`, `bianca_approve_update`, and `bianca_ignore_update`. Declare these connector capabilities in the artifact. Use only the viewing user's authorized connection.
>
> The HTML expects `window.biancaConnector.callTool(name, args)`. Implement that small adapter using the actual native artifact connector API available in this Claude account; do not invent an API signature. Install it before the page scripts initialize, or dispatch `bianca:connector-ready` afterward. Return plain JSON or standard MCP tool-result envelopes. Return failed operations as `isError: true` and include a safe `detail` string and HTTP-style numeric `status`.
>
> Keep the server as the source of truth. Do not replace the live queue with sample data, store company data in browser storage, put tokens into this HTML, or directly fetch an external backend from inside the artifact. If tools are missing or the connector is unavailable, show the existing connection-required state and stop the integration rather than simulating success.
>
> Verify reading the pending queue first. For the initial write test, use a designated non-production test company and a disposable test Google Sheet that the owner explicitly identifies. Confirm that approval places the exact former Next step in Last progress, writes the approved bullets into Next step, and does not touch other rows or columns. Verify that Edit applies the edited wording, Ignore leaves the sheet unchanged, NDL filtering works, and a repeated approval request cannot rotate the cells again.
>
> Share only with the intended Bianca founders inside the Enterprise organization after the connector and artifact permissions are confirmed. Do not publish this workspace publicly.

Official capability references:

- [Artifacts and connector behavior](https://code.claude.com/docs/en/artifacts#pull-live-data-with-mcp-connectors)
- [Custom remote connector setup](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)

This prompt configures the account-specific bridge. Committing the HTML to GitHub alone does not create or authorize an artifact in Claude Enterprise.
