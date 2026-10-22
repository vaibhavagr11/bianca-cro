from evals.scoring import profile_statements, rate, sheet_bullets


def test_sheet_bullets_split_cells_and_strip_markers():
    cells = ("- Proyecto new stores - Mandar follow-up\n- Forecast - they're using it\n\n  ok", "1. Move contract")
    assert sheet_bullets(*cells) == ["Proyecto new stores - Mandar follow-up", "Forecast - they're using it",
                                     "Move contract"]
    assert sheet_bullets(None, "") == []


def test_profile_statements_flatten_sections():
    profile = {
        "summary": {"text": "Pilot agreed.", "basis": "confirmed", "evidence": ["I1"]},
        "momentum_reason": {"text": "Fast.", "basis": "confirmed", "evidence": ["I2"]},
        "needs": [{"text": "Mapping.", "basis": "confirmed", "evidence": ["I3"]}],
        "open_commitments": [{"text": "Send forms", "owner": "NC", "due": None, "state": "open", "evidence": ["I4"]}],
        "recommended_actions": [{"action": "Sign NDA", "why": "Unblocks data", "urgency": "high", "evidence": ["I5"]}],
    }
    out = profile_statements(profile)
    assert [s["section"] for s in out] == ["summary", "momentum_reason", "needs", "open_commitments",
                                           "recommended_actions"]
    assert out[3]["text"] == "Send forms (owner: NC; due: -)"
    assert out[4]["text"] == "Recommended: Sign NDA. Why: Unblocks data"


def test_rate_strict_and_lenient():
    r = rate(["supported", "supported", "partly_supported", "unsupported"])
    assert r == {"n": 4, "supported": 2, "partly_supported": 1, "other": 1, "strict": 0.5, "lenient": 0.75}
    assert rate([])["strict"] is None
