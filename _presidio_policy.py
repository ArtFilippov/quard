"""Expected built-in English Presidio 2.2.360 / en_core_web_sm 3.8.0 policy."""
from __future__ import annotations


# Exact active registry, including the spaCy NER label mapping. A changed default
# registry must be reviewed rather than quietly narrowing or changing the policy.
ENGLISH_RECOGNIZERS = {
    "CreditCardRecognizer": ("CREDIT_CARD",),
    "CryptoRecognizer": ("CRYPTO",),
    "DateRecognizer": ("DATE_TIME",),
    "EmailRecognizer": ("EMAIL_ADDRESS",),
    "IbanRecognizer": ("IBAN_CODE",),
    "IpRecognizer": ("IP_ADDRESS",),
    "MedicalLicenseRecognizer": ("MEDICAL_LICENSE",),
    "NhsRecognizer": ("UK_NHS",),
    "PhoneRecognizer": ("PHONE_NUMBER",),
    "SpacyRecognizer": ("AGE", "DATE_TIME", "EMAIL", "ID", "LOCATION", "NRP",
                        "ORGANIZATION", "PERSON", "PHONE_NUMBER"),
    "UrlRecognizer": ("URL",),
    "UsBankRecognizer": ("US_BANK_NUMBER",),
    "UsItinRecognizer": ("US_ITIN",),
    "UsLicenseRecognizer": ("US_DRIVER_LICENSE",),
    "UsPassportRecognizer": ("US_PASSPORT",),
    "UsSsnRecognizer": ("US_SSN",),
}
ENGLISH_ENTITIES = frozenset(entity for entities in ENGLISH_RECOGNIZERS.values() for entity in entities)
ENGLISH_NER_ENTITIES = frozenset(ENGLISH_RECOGNIZERS["SpacyRecognizer"])


class PresidioPolicyError(RuntimeError):
    """The installed analyzer's effective English policy differs from the pin."""


def verify_english_inventory(engine) -> None:
    """Fail closed on missing, additional or inactive recognizers and NER labels."""
    expected = {(name, name, "en", tuple(entities)) for name, entities in ENGLISH_RECOGNIZERS.items()}

    def inventory(recognizers):
        return [(type(r).__name__, r.name, r.supported_language,
                 tuple(sorted(r.supported_entities))) for r in recognizers]

    registered = inventory(engine.registry.recognizers)
    active = inventory(engine.registry.get_recognizers(language="en", all_fields=True))
    if (len(registered) != len(expected) or set(registered) != expected or
            len(active) != len(expected) or set(active) != expected or
            set(engine.get_supported_entities(language="en")) != ENGLISH_ENTITIES or
            set(engine.nlp_engine.get_supported_entities()) != ENGLISH_NER_ENTITIES or
            engine.default_score_threshold != 0):
        raise PresidioPolicyError("Unexpected English Presidio inventory")
