"""Narrow Google Sheets adapter for founder-approved pipeline changes.

Only ``Last progress``, ``Next step`` and ``Client Context`` are writable. Every attempt resolves the
company and columns again, checks all reviewed cell values, and writes the fixed
before/after plan in one batch. Google Sheets has no cell compare-and-swap: a
collaborator can still edit or sort between the read and write. Keep the backend's
per-proposal lock and audit record, and avoid simultaneous manual row sorting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.config import settings
from app.pipeline import normalize_name

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUIRED_HEADERS = ("empresa", "last progress", "next step", "client context")


class SheetError(Exception):
    """A safe, actionable Sheets error, without provider response bodies or keys."""


class SheetConflict(SheetError, ValueError):
    """The live row is missing, ambiguous, or different from the reviewed row."""


class SheetWriteUncertain(SheetError):
    """A write may have succeeded; retry only the same persisted before/after plan."""


@dataclass(frozen=True)
class SheetRow:
    company: str
    last_progress: str
    next_step: str
    row_number: int  # One-based row number, including the header.
    sheet_id: int
    last_progress_col: int  # Zero-based column indices for updateCells.
    next_step_col: int
    client_context: str
    client_context_col: int


class _AuthResponse:
    def __init__(self, status: int, data: bytes, headers):
        self.status, self.data, self.headers = status, data, headers


class _AuthRequest:
    """google-auth's request protocol, implemented with bounded stdlib I/O."""

    def __init__(self, timeout: float):
        self.timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        request = Request(url, data=body, headers=headers or {}, method=method)
        try:
            response = urlopen(request, timeout=min(timeout or self.timeout, self.timeout))
        except HTTPError as exc:
            # google-auth inspects the status and body to classify token failures.
            return _AuthResponse(exc.code, exc.read(MAX_RESPONSE_BYTES), exc.headers)
        with response:
            return _AuthResponse(response.status, response.read(MAX_RESPONSE_BYTES), response.headers)


Transport = Callable[[str, str, dict, dict | None, float], dict]


def _json_transport(method: str, url: str, headers: dict, payload: dict | None, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    result = json.loads(body or b"{}")
    if not isinstance(result, dict):
        raise ValueError("invalid Sheets response")
    return result


def _display_text(cell: dict) -> str:
    """Read company names and headers; never use this to overwrite formulas."""
    for field in ("userEnteredValue", "effectiveValue"):
        value = cell.get(field) or {}
        if "stringValue" in value:
            return value["stringValue"]
    return str(cell.get("formattedValue") or "")


def _review_text(cell: dict) -> str:
    entered = cell.get("userEnteredValue") or {}
    effective = cell.get("effectiveValue") or {}
    if "formulaValue" in entered or "errorValue" in effective:
        raise SheetConflict("Review columns contain a formula or error. Resolve it in the Sheet before approving.")
    if any(key in entered or key in effective for key in ("numberValue", "boolValue")):
        raise SheetConflict("Last progress, Next step and Client Context must contain plain text before approving.")
    # Blank cells can have no userEnteredValue at all. Preserve literal whitespace.
    return entered.get("stringValue", effective.get("stringValue", ""))


class SheetsClient:
    def __init__(self, spreadsheet_id: str | None = None, sheet_tab: str | None = None,
                 credentials=None, transport: Transport | None = None, timeout: float = 20,
                 sheet_id: int | None = None):
        self.spreadsheet_id = spreadsheet_id or settings.review_spreadsheet_id
        self.sheet_tab = sheet_tab or settings.review_sheet_tab
        self.sheet_id = sheet_id if sheet_id is not None else settings.review_sheet_id
        self.credentials = credentials
        self.transport = transport or _json_transport
        self.timeout = max(1, min(float(timeout), 60))
        if not self.spreadsheet_id or not re.fullmatch(r"[A-Za-z0-9_-]+", self.spreadsheet_id):
            raise SheetError("REVIEW_SPREADSHEET_ID must identify the source-of-truth spreadsheet.")
        if not self.sheet_tab:
            raise SheetError("REVIEW_SHEET_TAB must identify the source-of-truth tab.")
        if self.sheet_id is not None and (isinstance(self.sheet_id, bool) or not isinstance(self.sheet_id, int) or self.sheet_id < 0):
            raise SheetError("REVIEW_SHEET_ID must be the numeric Pipeline tab ID.")

    def _headers(self) -> dict:
        try:
            if self.credentials is None:
                from google.oauth2 import service_account

                secret = settings.review_google_service_account_json
                if secret is None:
                    raise SheetError("Google Sheet approval is not configured. Set the service-account credential.")
                info = json.loads(secret.get_secret_value())
                self.credentials = service_account.Credentials.from_service_account_info(info, scopes=[SHEETS_SCOPE])
            if not self.credentials.valid:
                self.credentials.refresh(_AuthRequest(self.timeout))
            token = self.credentials.token
            if not isinstance(token, str) or not token:
                raise ValueError("no access token")
        except SheetError:
            raise
        except Exception:
            # Credential and token errors can include key material or HTTP bodies.
            raise SheetError("Google authentication failed. Check the service-account credential and sheet access.") from None
        return {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}

    def _request(self, method: str, suffix: str = "", payload: dict | None = None, query: dict | None = None) -> dict:
        url = f"{API_ROOT}/{quote(self.spreadsheet_id, safe='')}{suffix}"
        if query:
            url += "?" + urlencode(query)
        headers = self._headers()  # An auth failure occurs before any sheet write.
        try:
            result = self.transport(method, url, headers, payload, self.timeout)
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            return result
        except HTTPError as exc:
            if method == "POST" and (exc.code >= 500 or exc.code == 408):
                raise SheetWriteUncertain("Google did not confirm the write. Retry the same approval to reconcile its result.") from None
            raise SheetError("Google Sheets rejected the request. Check sheet access and configuration, then retry.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            if method == "POST":
                raise SheetWriteUncertain("The connection ended before the write was confirmed. Retry the same approval.") from None
            raise SheetError("Could not read the Google Sheet. Check connectivity and try again.") from None

    def read_company(self, company: str) -> SheetRow:
        """Resolve one current company row using headers and normalized Empresa.

        The full configured tab is read because its columns and rows may move.
        Existing numeric indices are never trusted on the next approval attempt.
        """
        if not isinstance(company, str) or not normalize_name(company):
            raise SheetConflict("The company name is missing or cannot be matched.")
        tab_range = "'" + self.sheet_tab.replace("'", "''") + "'"
        response = self._request("GET", query={
            "ranges": tab_range,
            "fields": "sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,formattedValue))))",
        })
        sheets = [s for s in response.get("sheets", []) if (s.get("properties") or {}).get("title") == self.sheet_tab]
        if len(sheets) != 1:
            raise SheetConflict("The configured Pipeline tab was not found uniquely. Check REVIEW_SHEET_TAB.")
        sheet = sheets[0]
        sheet_id = sheet.get("properties", {}).get("sheetId")
        if not isinstance(sheet_id, int):
            raise SheetError("Google returned an incomplete sheet. Try again before approving.")
        if self.sheet_id is not None and sheet_id != self.sheet_id:
            raise SheetConflict("The configured Pipeline tab ID does not match REVIEW_SHEET_ID. Check the target Sheet before approving.")
        cells: dict[int, dict[int, dict]] = {}
        for grid in sheet.get("data", []):
            for offset, row in enumerate(grid.get("rowData", []), start=grid.get("startRow", 0)):
                target = cells.setdefault(offset, {})
                for col, cell in enumerate(row.get("values", []), start=grid.get("startColumn", 0)):
                    target[col] = cell
        headers: dict[str, list[int]] = {}
        for col, cell in cells.get(0, {}).items():
            headers.setdefault(_display_text(cell).strip().casefold(), []).append(col)
        if any(len(headers.get(name, [])) != 1 for name in REQUIRED_HEADERS):
            raise SheetConflict("The first row must contain exactly one Empresa, Last progress, Next step, and Client Context header.")
        company_col, progress_col, next_col, context_col = (headers[name][0] for name in REQUIRED_HEADERS)
        matches = [(number, row) for number, row in cells.items() if number > 0
                   and normalize_name(_display_text(row.get(company_col, {}))) == normalize_name(company)]
        if len(matches) != 1:
            raise SheetConflict("The company must have exactly one matching Empresa row. Refresh the pipeline and resolve duplicates.")
        number, row = matches[0]
        return SheetRow(company=_display_text(row.get(company_col, {})),
                        last_progress=_review_text(row.get(progress_col, {})),
                        next_step=_review_text(row.get(next_col, {})), row_number=number + 1,
                        sheet_id=sheet_id, last_progress_col=progress_col, next_step_col=next_col,
                        client_context=_review_text(row.get(context_col, {})), client_context_col=context_col)

    def apply_update(self, company: str, expected_last_progress: str, expected_next_step: str,
                     new_last_progress: str, new_next_step: str, *,
                     expected_client_context: str, new_client_context: str) -> SheetRow:
        """Apply one persisted three-cell plan; retries never rotate or append twice."""
        before = (expected_last_progress, expected_next_step, expected_client_context)
        after = (new_last_progress, new_next_step, new_client_context)
        values = before + after
        if any(not isinstance(value, str) for value in values):
            raise SheetError("Approval cell values must be text.")
        if any(len(value) > 50_000 for value in after):
            raise SheetError("An approved cell exceeds Google's 50,000-character limit. Shorten the update.")
        row = self.read_company(company)
        current = (row.last_progress, row.next_step, row.client_context)
        if current == after:
            return row
        if current != before:
            raise SheetConflict("The Sheet changed since this card was prepared. Refresh the pipeline before approving.")
        requests = []
        for column, old_value, value in zip((row.last_progress_col, row.next_step_col, row.client_context_col), before, after):
            if old_value == value:
                continue
            requests.append({"updateCells": {
                "range": {"sheetId": row.sheet_id, "startRowIndex": row.row_number - 1,
                          "endRowIndex": row.row_number, "startColumnIndex": column, "endColumnIndex": column + 1},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": value}}]}],
                "fields": "userEnteredValue",
            }})
        # All changed cells are atomic within this batch. The preceding comparison is not
        # a server-side CAS and cannot exclude a simultaneous human edit/sort.
        self._request("POST", ":batchUpdate", payload={"requests": requests})
        try:
            verified = self.read_company(company)
        except (SheetError, ValueError):
            raise SheetWriteUncertain("The write was accepted but could not be verified. Retry the same approval.") from None
        if (verified.last_progress, verified.next_step, verified.client_context) != after:
            raise SheetWriteUncertain("The Sheet changed while the update was being verified. Reconcile the same approval before proceeding.")
        return verified
