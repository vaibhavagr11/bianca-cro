"""Mocked tests: no Google account, credentials, or network is used."""

from copy import deepcopy
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from app.review_sheets import SheetConflict, SheetError, SheetsClient, SheetWriteUncertain


class Credentials:
    valid = True
    token = "private-test-token"


def cell(value):
    if isinstance(value, dict):
        return value
    return {"userEnteredValue": {"stringValue": value}}


def document(rows=None, title="Pipeline", sheet_id=123, headers=None):
    headers = headers or ["Empresa", "Owner", "Last progress", "Next step", "Status"]
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
    return SheetsClient(spreadsheet_id="test-sheet", credentials=Credentials(), transport=transport, **kwargs), transport


def approve(api, **kwargs):
    return api.apply_update("Campari", "Old progress", "Previous next step", "Previous next step", "- Approved update", **kwargs)


def test_resolves_company_and_columns_after_sheet_reordering():
    data = document(headers=["Next step", "Empresa", "Status", "Last progress", "Owner"],
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
    api.apply_update("Campari", "Old progress", "Previous next step", "Previous next step", '=HYPERLINK("https://example.com")')
    assert transport.data["sheets"][0]["data"][0]["rowData"][1]["values"][3] == cell('=HYPERLINK("https://example.com")')


def test_empty_old_next_step_replaces_last_progress_with_empty_text():
    api, transport = client(document(rows=[["Campari", "RG", "Old progress", {}, "4. Pilot"]]))
    row = api.apply_update("Campari", "Old progress", "", "", "- New next step")
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
    api = SheetsClient(spreadsheet_id="test-sheet", credentials=Expired(), transport=transport, timeout=500)
    api.read_company("Campari")
    assert transport.calls[0][2]["Authorization"] == "Bearer refreshed-private-token"
    assert transport.calls[0][4] == 60


def test_nontext_and_oversized_approval_values_never_reach_google():
    api, transport = client()
    with pytest.raises(SheetError, match="text"):
        api.apply_update("Campari", "Old progress", "Previous next step", None, "- Next")
    with pytest.raises(SheetError, match="50,000"):
        api.apply_update("Campari", "Old progress", "Previous next step", "Previous next step", "x" * 50001)
    assert transport.calls == []
