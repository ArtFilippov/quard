"""Pinned English/Russian Presidio 2.2.360 and spaCy 3.8.0 inventory."""
from __future__ import annotations

import re

from _contextual_recognizers import add_contextual_recognizers


# Exact active registry, including the spaCy NER label mapping. A changed default
# registry must be reviewed rather than quietly narrowing or changing the policy.
ENGLISH_RECOGNIZERS = {
    "CreditCardRecognizer": ("CREDIT_CARD",),
    "CryptoRecognizer": ("CRYPTO",),
    "DateRecognizer": ("DATE_TIME",),
    "EmailRecognizer": ("EMAIL_ADDRESS",),
    "IbanRecognizer": ("IBAN_CODE",),
    "IpRecognizer": ("IP_ADDRESS",),
    "LocalPathRecognizer": ("LOCAL_PATH_IDENTIFIER",),
    "MedicalLicenseRecognizer": ("MEDICAL_LICENSE",),
    "NhsRecognizer": ("UK_NHS",),
    "PhoneRecognizer": ("PHONE_NUMBER",),
    "RemoteAccountRecognizer": ("REMOTE_ACCOUNT",),
    "SeedRecognizer": ("KNOWN_IDENTIFIER_SEED",),
    "IdentifierPatternRecognizer": ("KNOWN_IDENTIFIER_PATTERN",),
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
RUSSIAN_NER_ENTITIES = frozenset({"PERSON", "LOCATION", "ORGANIZATION"})
# The standard multi-language registry supplies eight shared pattern recognizers.
# Country-specific English patterns also apply to identifiers in Russian prose;
# Russian domestic identifiers have explicit patterns and context below.
RUSSIAN_RECOGNIZERS = {
    **{name: entities for name, entities in ENGLISH_RECOGNIZERS.items()
       if name != "SpacyRecognizer"},
    "SpacyRecognizer": tuple(sorted(RUSSIAN_NER_ENTITIES)),
    "RuInnRecognizer": ("RU_INN",),
    "RuSnilsRecognizer": ("RU_SNILS",),
    "RuPassportRecognizer": ("RU_PASSPORT",),
    "RuDateRecognizer": ("DATE_TIME",),
}
RUSSIAN_ENTITIES = frozenset(e for entities in RUSSIAN_RECOGNIZERS.values() for e in entities)
ALL_ENTITIES = ENGLISH_ENTITIES | RUSSIAN_ENTITIES


def add_russian_recognizers(engine, seeds: tuple[str, ...] = (),
                            patterns: tuple[re.Pattern[str], ...] = ()) -> None:
    """Extend Presidio's Russian defaults with country identifiers and NER parity."""
    from presidio_analyzer import Pattern, PatternRecognizer
    from presidio_analyzer import predefined_recognizers as builtins

    for recognizer in engine.registry.recognizers:
        if type(recognizer).__name__ == "SpacyRecognizer" and recognizer.supported_language == "ru":
            recognizer.supported_entities = sorted(RUSSIAN_NER_ENTITIES)
    country_context = {
        "CreditCardRecognizer": ["карта", "кредитная"],
        "NhsRecognizer": ["nhs", "медицинский"],
        "UsBankRecognizer": ["счёт", "банк"],
        "UsLicenseRecognizer": ["права", "водительское"],
        "UsItinRecognizer": ["itin", "налоговый"],
        "UsPassportRecognizer": ["паспорт", "passport"],
        "UsSsnRecognizer": ["ssn", "социального страхования"],
    }
    for name, context in country_context.items():
        engine.registry.add_recognizer(getattr(builtins, name)(supported_language="ru", context=context))
    for name, entity, regex, context in (
        ("RuInnRecognizer", "RU_INN", r"\b(?:\d{10}|\d{12})\b", ["инн"]),
        ("RuSnilsRecognizer", "RU_SNILS", r"\b\d{3}[ -]\d{3}[ -]\d{3} ?\d{2}\b", ["снилс"]),
        ("RuPassportRecognizer", "RU_PASSPORT", r"\b\d{4} \d{6}\b", ["паспорт"]),
        ("RuDateRecognizer", "DATE_TIME",
         r"\b(?:[0-3]?\d) (?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря) \d{4}(?: года)?\b",
         ["дата", "родился", "родилась"]),
    ):
        engine.registry.add_recognizer(PatternRecognizer(
            supported_entity=entity, name=name, supported_language="ru",
            patterns=[Pattern(name, regex, 0.3)], context=context))
    add_contextual_recognizers(engine, seeds, patterns)


class PresidioPolicyError(RuntimeError):
    """The installed analyzer's effective policy differs from the pin."""


def verify_inventory(engine) -> None:
    """Fail closed on missing, additional or inactive recognizers and NER labels."""
    expected = {(name, name, "en", tuple(entities)) for name, entities in ENGLISH_RECOGNIZERS.items()}
    expected |= {("PatternRecognizer" if name in {"RuInnRecognizer", "RuSnilsRecognizer",
                                                  "RuPassportRecognizer", "RuDateRecognizer"} else name,
                  name, "ru", tuple(entities))
                 for name, entities in RUSSIAN_RECOGNIZERS.items()}

    def inventory(recognizers):
        return [(type(r).__name__, r.name, r.supported_language,
                 tuple(sorted(r.supported_entities))) for r in recognizers]

    registered = inventory(engine.registry.recognizers)
    active = inventory(engine.registry.get_recognizers(language="en", all_fields=True))
    active_ru = inventory(engine.registry.get_recognizers(language="ru", all_fields=True))
    if (len(registered) != len(expected) or set(registered) != expected or
            len(active) != len(ENGLISH_RECOGNIZERS) or set(active) != {r for r in expected if r[2] == "en"} or
            len(active_ru) != len(RUSSIAN_RECOGNIZERS) or set(active_ru) != {r for r in expected if r[2] == "ru"} or
            set(engine.get_supported_entities(language="en")) != ENGLISH_ENTITIES or
            set(engine.get_supported_entities(language="ru")) != RUSSIAN_ENTITIES or
            set(engine.nlp_engine.get_supported_entities()) != ENGLISH_NER_ENTITIES or
            not engine.nlp_engine.nlp["ru"].has_pipe("ner") or
            set(engine.nlp_engine.nlp["ru"].get_pipe("ner").labels) != {"PER", "LOC", "ORG"} or
            {label: engine.nlp_engine.ner_model_configuration.model_to_presidio_entity_mapping.get(label)
             for label in ("PER", "LOC", "ORG")} !=
            {"PER": "PERSON", "LOC": "LOCATION", "ORG": "ORGANIZATION"} or
            engine.default_score_threshold != 0):
        raise PresidioPolicyError("Unexpected bilingual Presidio inventory")
