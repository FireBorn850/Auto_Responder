"""
Free-first language detection for incoming reviews.

langdetect runs locally (no API call, no cost). Only a German result gets a
second pass through Gemini, because langdetect can't tell Swiss German ("gsw")
from standard German and that difference changes how we reply.
"""
import re

from langdetect import detect, LangDetectException, DetectorFactory

from reviews.services.ai_responder import detect_review_language, EXTRA_LANGUAGES

# langdetect isn't deterministic run-to-run unless seeded.
DetectorFactory.seed = 0

# Fallback only, for text langdetect can't judge (e.g. "Super !"). Needs a
# *ratio* of hits so one loanword ("a la carte") doesn't flip the language.
_FRENCH_HINTS = re.compile(
    r"\b(le|la|les|un|une|des|est|très|nous|avons|été|pour|avec|c'est|qui|pas)\b",
    re.IGNORECASE,
)


def guess_language(text: str) -> str:
    """Language code for a review. Calls Gemini only for German text (de vs gsw)."""
    text = (text or "").strip()
    if not text:
        return "fr"

    try:
        detected = detect(text)
    except LangDetectException:
        detected = None

    if detected in ("fr", "en", "it"):
        return detected  # confident enough: no AI call
    if detected == "de":
        return detect_review_language(text, fallback_language="de")  # de vs Swiss German

    words = text.split()
    if detected in EXTRA_LANGUAGES and len(words) >= 4:
        return detected  # e.g. Spanish or Dutch: long enough to trust, still free
    hits = len(_FRENCH_HINTS.findall(text))
    return "fr" if (hits / len(words)) > 0.15 else "en"
