# War Room restyle (presentation layer only)

`patch_artifact.py` turns the original two pages (commit fe85969 on GSheet-Updates) into the War Room / Focus layout
that ships in `../sales-pipeline-updates.html` and `../sales-pipeline-updates-demo.html`.

    git show fe85969:artifacts/sales-pipeline-updates.html      > /tmp/orig/sales-pipeline-updates.html
    git show fe85969:artifacts/sales-pipeline-updates-demo.html > /tmp/orig/sales-pipeline-updates-demo.html
    python3 patch_artifact.py /tmp/orig ..

What it changes: `<style>`, header, toolbar (segmented owner control that drives the hidden `#owner-filter` select),
editor dialog extras (inert, labelled Proposed), a `card()` renderer, a shell around `#review-list` (sidebar, prev / next,
J / K / A / E / I keys), toast wording, and the demo seed (`seed_data.py`: 12 real companies from the Pipeline Sheet copy
as of Nov 3, 2026; client context lines illustrative).

What it does not touch: `BiancaLive` (tools, transports, workspace validation, decide / refresh), `BiancaCore`
approve / ignore / valid, `window.biancaConnector.callTool`, `bianca:connector-ready`, the hosted token form,
`displayTrace` / `bulletList`, dialog payloads, tab roles.

`SHEET_URL` at the top of the script is the only constant to change (today: the staging Sheet copy).
The live page keeps no browser storage; the demo page remembers its theme in `localStorage`. `?theme=focus|war-room`
works on both.

## Tests

`tests-frontend/verify-demo.cjs` and `verify-live.cjs` assert the original markup. After the restyle, 45 / 55 live and
23 / 36 demo cases pass. Every remaining failure is an intentional presentation change, not a behaviour change:

- source chips (`data-action="source"`, `data-source-unavailable`) and bullet highlighting are gone; a card shows a
  one-line trace note instead when lineage is unavailable or unattributed
- `Current Next steps` / `Current Client Context` dropdowns are one `Current Sheet cells` disclosure
- the demo seed is 12 real companies, not the 6 fictional ones the demo tests enumerate
- the NDL filter reads `Nicolás de León`, the toast reads `Written to the Sheet copy`
- the War Room theme renders one card at a time; the headless tests see the Focus (all cards) layout because
  `theme()` falls back to it when there is no `document.body`

The behavioural checks that still pass include: approve / ignore semantics, revision and idempotency handling,
applying / retry recovery, mirror-sync notice flow, lineage validation, empty live page, no credentials in HTML.
