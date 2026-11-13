"""Recognize profile suggestions whose only action is a nudge."""

import re
import unicodedata


_NUDGE = (
    r"follow[ -]up|check (?:in|back)|circle back|nudge|chase|remind|ask again"
    r"|(?:send|write) (?:a|another) (?:gentle )?(?:reminder|follow[ -]up)"
    r"|(?:dar(?:le|les)?|hacer) seguimiento|volver a (?:preguntar|pedir|consultar|escribir)"
    r"|recordar(?:le|les)?|(?:enviar|mandar) (?:un|otro) recordatorio"
)
_OUTREACH = (
    r"(?:email|message) (?!the |a |an )\w+(?= to )"
    r"|(?:email|message) (?:the )?(?:buyer|customer|client|prospect|contact|team)(?= to )"
    r"|call|contact|reach out to|write to|escribir(?:le|les)? a|contactar a|llamar a"
)
_FOLLOWUP = re.compile(rf"^(?:(?:{_OUTREACH})\b[^.!?;]*?\b(?:to|para) )?(?:{_NUDGE})\b")

# A coordinated action or a delivery introduced as the means of following up can still be a next move.
_DELIVERY = (
    r"(?:send(?:ing)?|shar(?:e|ing)|enviar|enviando|mandar|mandando|compartir|compartiendo)\b"
    r"(?! (?:a |an |the |another |un |el |otro )?(?:gentle )?(?:reminder|nudge|follow[ -]up|recordatorio)\b)"
)
_OTHER_WORK = (
    r"(?:prepar(?:e|ing|ar|ando)|deliver(?:ing)?|draft(?:ing)?|complet(?:e|ing|ar|ando)"
    r"|schedul(?:e|ing)|updat(?:e|ing)|attach(?:ing)?|offer(?:ing)?|submit(?:ting)?"
    r"|entregar|entregando|redactar|redactando|agendar|agendando|actualizar|actualizando)\b"
)
_NEW_WORK = re.compile(
    rf"(?:[.!?;:]\s*|\b(?P<connector>and(?: then)?|then|also|by|to|y(?: luego)?|luego|tambien|para) )"
    rf"(?:{_DELIVERY}|{_OTHER_WORK})"
)
_SPANISH_MEANS = re.compile(r"\b(?:enviando|mandando|compartiendo|preparando|entregando)\b")
_RECIPIENT_REQUEST = re.compile(r"\b(?:ask|remind|prompt) (?:them|him|her|the [\w -]+) $")


def is_followup_only(text: str) -> bool:
    """Flag clear nudge-only suggestions, leaving ambiguous recommendations eligible."""
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = " ".join("".join(c for c in normalized if not unicodedata.combining(c)).split())
    normalized = normalized.lstrip("-• ")
    if not _FOLLOWUP.match(normalized) or _SPANISH_MEANS.search(normalized):
        return False
    for match in _NEW_WORK.finditer(normalized):
        if match["connector"] != "to" or not _RECIPIENT_REQUEST.search(normalized[:match.start()]):
            return False
    return True
