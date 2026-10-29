from datetime import date

import pytest

from app.gmail import REDACTED, redact_secrets
from app.notes import clean_body, is_meeting_header, parse_header_date, recent_sections, split_sections

TODAY = date(2026, 10, 21)
GAP = "\n\n\n\n\n"

# Made-up notes in the doc's real header styles: newest first, blank-line runs between meetings.
DOC = GAP.join([
    "ACME 08-10\nNDL, RG\n\n-Pilot going well\n\n\n-Two blank lines stay inside the meeting",
    "Globex <> Bianca\nAgenda:\nPricing review",
    "Initech <> Bianca Reestructurador 7 oct 2026 11 am\nBren, RG",
    "Umbrella 06-10\n\nAdd export button",
    "En esta descarga agregar columnas por sucursal y mucho texto que sigue la reunion anterior",
    "Resumen\nThis chunk continues Umbrella too",
    "Hooli, 05 October 2026\nJane, NC\n\nResumen:\nContraseña admin hooli: 1234",
    "Stark, 29 de septiembre de 2026\nTony\n\nNotes",
    "Wayne 17/09\nOld meeting",
    "ACME 08-10\nAn older ACME meeting from last year? no, same day-month earlier in doc order",
])


@pytest.mark.parametrize(
    "lines, expected",
    [
        (["REBESA 08-10"], date(2026, 10, 8)),
        (["FEMSA 6 Oct 2026"], date(2026, 10, 6)),
        (["Smearcase <> Bianca, 05 October 2026"], date(2026, 10, 5)),
        (["Black Pampa, 29 de septiembre de 2026"], date(2026, 9, 29)),
        (["Delta <> Bianca Catchup Lunes 5 de Oct 2pm"], date(2026, 10, 5)),
        (["FEMSA <> Bianca Martes 29 de Septiembre 11 AM"], date(2026, 9, 29)),
        (["Campari 17/09"], date(2026, 9, 17)),
        (["Ron", "29-09"], date(2026, 9, 29)),
        (["Koala Eco <> Bianca Wed 26 August 2026 12 PM"], date(2026, 8, 26)),
        (["Gus <> Ramiro"], None),
        (["Pitch at 10:30"], None),
    ],
)
def test_parse_header_date(lines, expected):
    assert parse_header_date(lines, TODAY) == expected


def test_no_year_date_in_future_rolls_back_a_year():
    assert parse_header_date(["Meeting 15-12"], TODAY) == date(2025, 12, 15)


def test_split_merges_continuations_and_keeps_inner_blank_lines():
    sections = split_sections(DOC, TODAY)
    headers = [s.header for s in sections]
    assert headers[:5] == ["ACME 08-10", "Globex <> Bianca", "Initech <> Bianca Reestructurador 7 oct 2026 11 am",
                           "Umbrella 06-10", "Hooli, 05 October 2026"]
    assert "Two blank lines stay inside" in sections[0].text
    umbrella = sections[3]
    assert "En esta descarga" in umbrella.text and "continues Umbrella" in umbrella.text


def test_undated_section_inherits_date_from_meeting_below():
    globex = split_sections(DOC, TODAY)[1]
    assert globex.meeting_date == date(2026, 10, 7) and globex.date_inferred


def test_ids_are_stable_when_new_meetings_are_added_on_top():
    before = {s.header + s.text[:20]: s.section_id for s in split_sections(DOC, TODAY)}
    after = {s.header + s.text[:20]: s.section_id
             for s in split_sections("Newco <> Bianca 20 oct 2026\nKickoff" + GAP + DOC, TODAY)}
    assert all(after[k] == v for k, v in before.items())


def test_same_header_twice_gets_distinct_ids():
    ids = [s.section_id for s in split_sections(DOC, TODAY) if s.header == "ACME 08-10"]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_recent_sections_stop_at_first_explicit_old_date():
    recent = recent_sections(split_sections(DOC, TODAY), TODAY, 30)
    assert [s.header.split()[0] for s in recent] == ["ACME", "Globex", "Initech", "Umbrella", "Hooli,", "Stark,"]


def test_clean_body_redacts_passwords():
    hooli = split_sections(DOC, TODAY)[4]
    body = clean_body(hooli)
    assert "1234" not in body and REDACTED in body


@pytest.mark.parametrize("text", ["Esto es clave para el negocio", "Usuario final compra más", "PIN the meeting"])
def test_password_redaction_ignores_normal_words(text):
    assert redact_secrets(text) == text


# Two meetings with only one or two blank lines between them (the doc's real merged cases, made up).
MERGED = GAP.join([
    "Gus RG 15-10\n\nCampaña de kioscos\n\n\nGlobex <> Bianca Fri 16 Oct 2026\n-Next steps",
    "Paolo\n-escribir a todos los de la uni\n\n\nAcme Shower Head, 19 Agosto 2026\nNotes on the pilot",
    "Tomi, Agus\n-route pictures\n\nVista Wines, 10 de Septiembre de 2026\nPablo\n\n"
    "Hooli Foods <> Bianca AI Wed 9 Sep 1:05 PM\nJohnny",
    "Roger 06-10\n-descargable\n\n\nFEMSA 6 Oct 2026\n\n-Estamos matando mayoristas",
])


def test_dated_header_after_a_blank_line_starts_a_new_meeting():
    headers = [s.header for s in split_sections(MERGED, TODAY)]
    assert headers == ["Gus RG 15-10", "Globex <> Bianca Fri 16 Oct 2026", "Paolo", "Acme Shower Head, 19 Agosto 2026",
                       "Tomi, Agus", "Vista Wines, 10 de Septiembre de 2026",
                       "Hooli Foods <> Bianca AI Wed 9 Sep 1:05 PM", "Roger 06-10", "FEMSA 6 Oct 2026"]
    acme = next(s for s in split_sections(MERGED, TODAY) if s.header.startswith("Acme"))
    assert acme.meeting_date == date(2026, 8, 19) and "uni" not in acme.text


@pytest.mark.parametrize("line", [
    "Adding Amazon y otro DTCs (6/7?)", "Re-scans take 2/4 business days", "Semi-activos: 1-2 compras",
    "A16z (es el evento principal de tech), 6,7,8 de agosto", "Ejemplo fecha 20/08 fue reactivado",
    "-Circle back 30 de septiembre", "Resumen 9 Sep 2026", "Encompass <> Bianca",
])
def test_lines_that_mention_a_date_are_not_meeting_headers(line):
    assert not is_meeting_header(line, TODAY)


def test_existing_doc_splits_the_same():
    assert [s.header for s in split_sections(DOC, TODAY)] == [
        "ACME 08-10", "Globex <> Bianca", "Initech <> Bianca Reestructurador 7 oct 2026 11 am", "Umbrella 06-10",
        "Hooli, 05 October 2026", "Stark, 29 de septiembre de 2026", "Wayne 17/09", "ACME 08-10"]
