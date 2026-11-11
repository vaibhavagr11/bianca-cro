"""Mocked tests: no Google account, credentials, or network is used."""

from copy import deepcopy
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr

from app.review_sheets import SheetConflict, SheetError, SheetsClient, SheetWriteUncertain


class Credentials:
    valid = True
    token = "private-test-token"


def cell(value):
    if isinstance(value, dict):
        return value
    return {"userEnteredValue": {"stringValue": value}}


def document(rows=None, title="Pipeline", sheet_id=123, headers=None):
    headers = headers or ["Empresa", "Owner", "Last progress", "Next step", "Status", "Client Context"]
    rows = rows if rows is not None else [["Campari", "RG", "Old progress", "Previous next step", "4. Pilot"]]
    return {"sheets": [{"properties": {"title": title, "sheetId": sheet_id},
                        "data": [{"rowData": [{"values": [cell(v) for v in r]} for r in [headers, *rows]]}]}]}


class Transport:
    def __init__(self, data):
        self.data, self.calls = deepcopy(data), []
        self.fail_write = None
        self.fail_verify = False
        self.change_after_write = False

    def __call__(self, method, url, headers, payload, timeout):
        self.calls.append((method, url, headers, payload, timeout))
        if method == "GET":
            if self.fail_verify and any(c[0] == "POST" for c in self.calls):
                raise URLError("private-test-token should never be shown")
            return deepcopy(self.data)
        if self.fail_write:
            raise self.fail_write
        for request in payload["requests"]:
            update = request["updateCells"]
            r, c = update["range"]["startRowIndex"], update["range"]["startColumnIndex"]
            rows = self.data["sheets"][0]["data"][0]["rowData"]
            while len(rows[r]["values"]) <= c:
                rows[r]["values"].append({})
            rows[r]["values"][c] = deepcopy(update["rows"][0]["values"][0])
        if self.change_after_write:
            self.data["sheets"][0]["data"][0]["rowData"][1]["values"][3] = cell("Concurrent edit")
        return {"replies": [{} for _ in payload["requests"]]}


def client(data=None, **kwargs):
    transport = Transport(data or document())
    return SheetsClient(spreadsheet_id="test-sheet", credentials=Credentials(), transport=transport,
                        **{"sheet_id": 123, **kwargs}), transport


def approve(api, **kwargs):
    kwargs = {"expected_client_context": "", "new_client_context": "", **kwargs}
    return api.apply_update("Campari", "Old progress", "Previous next step", "Previous next step", "- Approved update", **kwargs)


def test_resolves_company_and_columns_after_sheet_reordering():
    data = document(headers=["Next step", "Empresa", "Status", "Last progress", "Owner", "Client Context"],
                    rows=[["Other next", "Other company", "4. Pilot", "Other last", "NC"],
                          ["Previous next step", "Cámpári  ", "4. Pilot", "Old progress", "RG"]])
    api, transport = client(data)
    row = api.read_company(" campari ")
    assert (row.row_number, row.last_progress_col, row.next_step_col) == (3, 3, 0)
    assert row.company == "Cámpári  "
    assert row.last_progress == "Old progress"
    assert len(transport.calls) == 1


def test_approval_writes_only_two_cells_in_one_atomic_batch_and_verifies():
    api, transport = client()
    row = approve(api)
    assert (row.last_progress, row.next_step) == ("Previous next step", "- Approved update")
    assert [call[0] for call in transport.calls] == ["GET", "POST", "GET"]
    post = transport.calls[1]
    assert post[1].endswith("/test-sheet:batchUpdate")
    assert post[4] == 20
    assert len(post[3]["requests"]) == 2
    for n, value in enumerate(["Previous next step", "- Approved update"], start=2):
        update = post[3]["requests"][n - 2]["updateCells"]
        assert update["range"] == {"sheetId": 123, "startRowIndex": 1, "endRowIndex": 2,
                                    "startColumnIndex": n, "endColumnIndex": n + 1}
        assert update["rows"] == [{"values": [{"userEnteredValue": {"stringValue": value}}]}]
        assert update["fields"] == "userEnteredValue"
    assert transport.data["sheets"][0]["data"][0]["rowData"][1]["values"][1] == cell("RG")


def test_approval_uses_current_row_position_not_a_cached_number():
    api, transport = client()
    api.read_company("Campari")
    rows = transport.data["sheets"][0]["data"][0]["rowData"]
    rows.insert(1, {"values": [cell(v) for v in ["Other company", "NC", "Keep last", "Keep next", "4. Pilot"]]})
    row = approve(api)
    assert row.row_number == 3
    write = next(c for c in transport.calls if c[0] == "POST")
    assert all(r["updateCells"]["range"]["startRowIndex"] == 2 for r in write[3]["requests"])
    assert rows[1]["values"][3] == cell("Keep next")


def test_literal_formula_like_content_is_never_executed():
    api, transport = client()
    api.apply_update("Campari", "Old progress", "Previous next step", "Previous next step", '=HYPERLINK("https://example.com")', expected_client_context='', new_client_context='')
    assert transport.data["sheets"][0]["data"][0]["rowData"][1]["values"][3] == cell('=HYPERLINK("https://example.com")')


def test_empty_old_next_step_replaces_last_progress_with_empty_text():
    api, transport = client(document(rows=[["Campari", "RG", "Old progress", {}, "4. Pilot"]]))
    row = api.apply_update("Campari", "Old progress", "", "", "- New next step", expected_client_context="", new_client_context="")
    assert row.last_progress == ""
    assert row.next_step == "- New next step"


def test_retry_after_success_does_not_write_or_rotate_again():
    api, transport = client(document(rows=[["Campari", "RG", "Previous next step", "- Approved update", "4. Pilot"]]))
    assert approve(api).last_progress == "Previous next step"
    assert [c[0] for c in transport.calls] == ["GET"]


@pytest.mark.parametrize("last,next_step", [("Changed progress", "Previous next step"), ("Old progress", "Someone edited Next step")])
def test_concurrent_cell_changes_are_rejected_without_a_write(last, next_step):
    api, transport = client(document(rows=[["Campari", "RG", last, next_step, "4. Pilot"]]))
    with pytest.raises(SheetConflict, match="changed since"):
        approve(api)
    assert [c[0] for c in transport.calls] == ["GET"]


@pytest.mark.parametrize("rows", [[], [["Campari", "RG", "Old progress", "Previous next step"], ["cámpári", "RG", "Another last", "Another next"]]])
def test_missing_and_ambiguous_companies_are_not_written(rows):
    api, transport = client(document(rows=rows))
    with pytest.raises(SheetConflict, match="exactly one matching"):
        approve(api)
    assert all(c[0] == "GET" for c in transport.calls)


@pytest.mark.parametrize("headers", [["Empresa", "Owner", "Last progress", "Wrong header"], ["Empresa", "Next step", "Last progress", "Next step"]])
def test_missing_or_duplicate_required_headers_are_rejected(headers):
    api, transport = client(document(headers=headers))
    with pytest.raises(SheetConflict, match="first row"):
        approve(api)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("cell_data", [
    {"userEnteredValue": {"formulaValue": '=IF(TRUE,"text","")'}, "effectiveValue": {"stringValue": "Previous next step"}},
    {"userEnteredValue": {"numberValue": 123}, "formattedValue": "123"},
    {"userEnteredValue": {"boolValue": True}},
    {"effectiveValue": {"errorValue": {"type": "REF"}}},
])
def test_formulas_or_nontext_review_cells_are_not_silently_overwritten(cell_data):
    api, transport = client(document(rows=[["Campari", "RG", "Old progress", cell_data]]))
    with pytest.raises(SheetConflict):
        approve(api)
    assert all(c[0] == "GET" for c in transport.calls)


def test_sheet_tab_is_quoted_and_url_encoded():
    api, transport = client(document(title="Founder's Pipeline"), sheet_tab="Founder's Pipeline")
    api.read_company("Campari")
    query = parse_qs(urlparse(transport.calls[0][1]).query)
    assert query["ranges"] == ["'Founder''s Pipeline'"]
    assert "userEnteredValue" in query["fields"][0]


@pytest.mark.parametrize("error", [URLError("private-test-token"), TimeoutError("private-test-token"), HTTPError("secret-url", 503, "private-test-token", {}, None)])
def test_lost_or_ambiguous_write_response_retains_uncertain_state_without_leaking_details(error):
    api, transport = client()
    transport.fail_write = error
    with pytest.raises(SheetWriteUncertain) as exc:
        approve(api)
    assert "private-test-token" not in str(exc.value)


def test_rejected_permission_write_is_safe_and_not_reported_as_success():
    api, transport = client()
    transport.fail_write = HTTPError("secret-url", 403, "private-test-token", {}, None)
    with pytest.raises(SheetError) as exc:
        approve(api)
    assert not isinstance(exc.value, SheetWriteUncertain)
    assert "private-test-token" not in str(exc.value)


@pytest.mark.parametrize("mode", ["failed_read", "changed_cell"])
def test_after_write_verification_failure_is_uncertain(mode):
    api, transport = client()
    transport.fail_verify = mode == "failed_read"
    transport.change_after_write = mode == "changed_cell"
    with pytest.raises(SheetWriteUncertain):
        approve(api)


def test_credential_refresh_is_injected_and_uses_a_bounded_request():
    class Expired:
        valid = False
        token = None

        def refresh(self, request):
            assert request.timeout == 60
            self.valid, self.token = True, "refreshed-private-token"

    transport = Transport(document())
    api = SheetsClient(spreadsheet_id="test-sheet", credentials=Expired(), transport=transport, timeout=500, sheet_id=123)
    api.read_company("Campari")
    assert transport.calls[0][2]["Authorization"] == "Bearer refreshed-private-token"
    assert transport.calls[0][4] == 60


def test_nontext_and_oversized_approval_values_never_reach_google():
    api, transport = client()
    with pytest.raises(SheetError, match="text"):
        api.apply_update("Campari", "Old progress", "Previous next step", None, "- Next", expected_client_context="", new_client_context="")
    with pytest.raises(SheetError, match="50,000"):
        api.apply_update("Campari", "Old progress", "Previous next step", "Previous next step", "x" * 50001, expected_client_context="", new_client_context="")
    assert transport.calls == []


def test_routed_approval_updates_three_cells_and_preserves_unrelated_cells():
    api, transport = client(document(rows=[["Campari", "RG", "Old progress", "Previous next step", "4. Pilot", "Existing context"]]))
    row = approve(api, expected_client_context="Existing context", new_client_context="Existing context\n- New customer need")
    assert row.client_context == "Existing context\n- New customer need"
    post = next(call for call in transport.calls if call[0] == "POST")
    assert len(post[3]["requests"]) == 3
    assert {request["updateCells"]["range"]["startColumnIndex"] for request in post[3]["requests"]} == {2, 3, 5}
    assert transport.data["sheets"][0]["data"][0]["rowData"][1]["values"][4] == cell("4. Pilot")


def test_context_only_approval_writes_one_cell_without_rotating_next_step():
    api, transport = client()
    row = api.apply_update("Campari", "Old progress", "Previous next step", "Old progress", "Previous next step",
                           expected_client_context="", new_client_context="- Customer need: Better reporting.")
    post = next(call for call in transport.calls if call[0] == "POST")
    assert len(post[3]["requests"]) == 1
    assert post[3]["requests"][0]["updateCells"]["range"]["startColumnIndex"] == 5
    assert (row.last_progress, row.next_step) == ("Old progress", "Previous next step")


def test_context_conflict_rejects_action_write_too():
    api, transport = client(document(rows=[["Campari", "RG", "Old progress", "Previous next step", "4. Pilot", "Human changed context"]]))
    with pytest.raises(SheetConflict, match="changed since"):
        approve(api, expected_client_context="Prior context", new_client_context="Prior context\n- New context")
    assert all(call[0] == "GET" for call in transport.calls)


def test_completed_three_cell_retry_does_not_append_or_rotate_again():
    api, transport = client(document(rows=[["Campari", "RG", "Previous next step", "- Approved update", "4. Pilot", "Original\n- New context"]]))
    row = approve(api, expected_client_context="Original", new_client_context="Original\n- New context")
    assert row.client_context == "Original\n- New context"
    assert [call[0] for call in transport.calls] == ["GET"]


def test_moved_context_column_is_resolved_case_insensitively():
    api, transport = client(document(headers=[" CLIENT CONTEXT ", "Next Step", "Empresa", "LAST PROGRESS"],
                                     rows=[["Keep original", "Previous next step", "Campari", "Old progress"]]))
    row = approve(api, expected_client_context="Keep original", new_client_context="- Approved client context")
    assert (row.client_context_col, row.next_step_col, row.last_progress_col) == (0, 1, 3)
    assert row.client_context == "- Approved client context"
    post = next(call for call in transport.calls if call[0] == "POST")
    assert {request["updateCells"]["range"]["startColumnIndex"] for request in post[3]["requests"]} == {0, 1, 3}


def test_matching_tab_name_with_wrong_gid_is_rejected_before_write():
    api, transport = client(document(sheet_id=999))
    with pytest.raises(SheetConflict, match="tab ID"):
        approve(api)
    assert [call[0] for call in transport.calls] == ["GET"]


def test_default_target_matches_the_authorized_pipeline_sheet():
    api = SheetsClient(credentials=Credentials(), transport=Transport(document()))
    assert api.spreadsheet_id == "14lXRsWoEY63tOsz4SwggiItgTNPFWI4KDVTLay3MLDw"
    assert api.sheet_tab == "Pipeline"
    assert api.sheet_id == 1869050409


def test_missing_client_context_header_is_not_silently_ignored():
    api, transport = client(document(headers=["Empresa", "Owner", "Last progress", "Next step"]))
    with pytest.raises(SheetConflict, match="Client Context"):
        approve(api)
    assert all(call[0] == "GET" for call in transport.calls)


@pytest.mark.parametrize("context", [
    {"userEnteredValue": {"formulaValue": '=A1'}, "effectiveValue": {"stringValue": "Rendered"}},
    {"userEnteredValue": {"numberValue": 42}},
])
def test_invalid_context_cell_stops_the_entire_approval(context):
    api, transport = client(document(rows=[["Campari", "RG", "Old progress", "Previous next step", "4. Pilot", context]]))
    with pytest.raises(SheetConflict):
        approve(api)
    assert all(call[0] == "GET" for call in transport.calls)


def test_literal_context_is_written_as_string_and_not_evaluated():
    api, transport = client()
    row = approve(api, new_client_context='=HYPERLINK("https://example.com")')
    assert row.client_context == '=HYPERLINK("https://example.com")'
    context_cell = transport.data["sheets"][0]["data"][0]["rowData"][1]["values"][5]
    assert context_cell == cell('=HYPERLINK("https://example.com")')


# ---------------------------------------------------------------- through n8n (the Google connector)

class N8n:
    """Plays the n8n proxy workflow: checks the secret, forwards to fake Google, wraps the reply."""

    def __init__(self, google, status=None):
        self.google, self.status, self.calls = google, status, []

    def __call__(self, method, url, headers, payload, timeout):
        self.calls.append((method, url, headers, payload))
        assert headers["X-Bianca-Sheets-Secret"] == "proxy-secret"
        if self.status:
            return {"status": self.status, "body": {"error": {"message": "from Google"}}}
        google_url = "https://sheets.googleapis.com/v4/spreadsheets/" + payload["spreadsheetId"] + payload["suffix"]
        return {"status": 200, "body": self.google(payload["method"], google_url, {}, payload["body"], timeout)}


@pytest.fixture
def via_n8n(monkeypatch):
    from app.review_sheets import settings
    monkeypatch.setattr(settings, "review_sheets_webhook_url", "https://n8n.test/webhook/bianca-sheets")
    monkeypatch.setattr(settings, "review_sheets_webhook_secret", SecretStr("proxy-secret"))
    monkeypatch.setattr(settings, "review_google_service_account_json", None)

    def make(status=None):
        google = Transport(document())
        proxy = N8n(google, status)
        return SheetsClient(spreadsheet_id="test-sheet", transport=proxy, sheet_id=123), proxy, google
    return make


def test_n8n_path_reads_and_writes_without_a_google_credential(via_n8n):
    api, proxy, google = via_n8n()
    row = approve(api)
    assert (row.last_progress, row.next_step) == ("Previous next step", "- Approved update")
    assert [p["method"] for _, _, _, p in proxy.calls] == ["GET", "POST", "GET"]
    assert all(url == "https://n8n.test/webhook/bianca-sheets" and "Authorization" not in h for _, url, h, _ in proxy.calls)
    assert {p["spreadsheetId"] for _, _, _, p in proxy.calls} == {"test-sheet"}
    assert proxy.calls[1][3]["suffix"] == ":batchUpdate" and proxy.calls[0][3]["query"]["ranges"] == "'Pipeline'"


def test_n8n_path_maps_google_errors_like_direct_calls(via_n8n):
    api, _, _ = via_n8n(status=403)
    with pytest.raises(SheetError) as read_error:
        api.read_company("Campari")
    assert not isinstance(read_error.value, SheetWriteUncertain) and "from Google" not in str(read_error.value)

    api, proxy, google = via_n8n()
    proxy.status = None
    original = proxy.__call__

    def write_fails(method, url, headers, payload, timeout):
        if payload["method"] == "POST":
            return {"status": 503, "body": {}}
        return original(method, url, headers, payload, timeout)
    api.transport = write_fails
    with pytest.raises(SheetWriteUncertain):
        approve(api)


def test_n8n_path_treats_a_broken_reply_to_a_write_as_uncertain(via_n8n):
    api, proxy, _ = via_n8n()
    original = proxy.__call__
    api.transport = lambda m, u, h, p, t: "not json" if p["method"] == "POST" else original(m, u, h, p, t)
    with pytest.raises(SheetWriteUncertain):
        approve(api)


def test_n8n_path_needs_its_secret_before_any_call(via_n8n, monkeypatch):
    from app.review_sheets import settings
    api, proxy, _ = via_n8n()
    monkeypatch.setattr(settings, "review_sheets_webhook_secret", None)
    with pytest.raises(SheetError):
        api.read_company("Campari")
    assert proxy.calls == []
