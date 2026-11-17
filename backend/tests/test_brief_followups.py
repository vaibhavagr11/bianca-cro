import pytest

from app.brief_followups import is_followup_only


@pytest.mark.parametrize("text", [
    "Check in with the buyer on the proposal we sent last week.",
    "Ask again for proposal approval",
    "Follow up with Acme to get feedback",
    "Email Zach to follow up on the store data.",
    "Email the buyer to ask again for feedback.",
    "Follow up by sending a reminder to the buyer.",
    "Dar seguimiento con Compras para conocer su decisión.",
    "Escribirle a Ana para hacer seguimiento de la propuesta.",
    "Volver a preguntar por la aprobación del piloto.",
])
def test_detects_suggestions_that_only_nudge(text):
    assert is_followup_only(text)


@pytest.mark.parametrize("text", [
    "Send the revised pricing sheet and follow up on approval.",
    "Follow up by sending the revised pricing sheet.",
    "Email the revised pricing sheet to follow up on approval.",
    "Follow up with the buyer and send the revised pricing sheet.",
    "Email Zach to follow up on the store data. Prepare a template.",
    "Follow up with the buyer to schedule a kickoff.",
    "Ask the buyer to approve the updated proposal.",
    "Dar seguimiento enviando el presupuesto actualizado.",
    "Dar seguimiento para enviar el presupuesto actualizado.",
    "Enviar el presupuesto actualizado y hacer seguimiento.",
])
def test_preserves_suggestions_with_a_separate_next_move(text):
    assert not is_followup_only(text)
