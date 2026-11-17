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


@pytest.mark.parametrize("text", [
    "Keep following up with the buyer",                       # the bullet Nico read out on 11/12
    "The team should keep following up with Juan on the store data.",
    "Continue to follow up with procurement",
    "Bianca should keep checking in with Ana",
    "Keep chasing Franco for the signed NDA",
])
def test_keep_following_up_in_any_voice_is_a_nudge(text):
    assert is_followup_only(text)


@pytest.mark.parametrize("text", [
    "Keep the pricing proposal updated and send it to Ana",
    "Continue the pilot scoping with the new regional lead",
    "The team should prepare the rollout plan and follow up",
])
def test_ongoing_work_is_not_a_nudge(text):
    assert not is_followup_only(text)


# ---- B-02 (11/12): a row that "literally says no reply since" is a waiting state, not a deal to move

from app.brief_followups import is_waiting_only  # noqa: E402


@pytest.mark.parametrize("text", [
    "Wait for Franco's reply on the proposal",
    "Waiting on the buyer to confirm the pilot dates.",
    "No reply since Oct 21",
    "No response from procurement yet",
    "Franco has not replied to the Oct 21 proposal",
    "The buyer hasn't responded since the demo",
    "Still waiting for the signed NDA",
    "Pending their reply on pricing",
    "Await customer feedback on the deck",
    "Esperar respuesta de Compras",
    "Sin respuesta desde el 21 de octubre",
    "Juan no ha respondido el correo",
    "The customer went quiet after the pricing call",
])
def test_statements_that_only_say_we_are_waiting(text):
    assert is_waiting_only(text)


@pytest.mark.parametrize("text", [
    "Wait for the NDA, then send the pricing sheet",
    "Send the revised pricing sheet",
    "No budget approved for this year",
    "Ask the buyer to approve the updated proposal.",
    "Legal has not approved the NDA wording we need to change",
    "Prepare the waiting-list analysis for the buyer",
])
def test_real_work_or_other_blockers_are_not_waiting_statements(text):
    assert not is_waiting_only(text)
