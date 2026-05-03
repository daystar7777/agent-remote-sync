from __future__ import annotations

import os
import platform
import unicodedata

from .common_types import FilenamePolicy


DEFAULT_WIRE_FORM = "NFC"
VALID_FORMS = {"NFC", "NFD", "NFKC", "NFKD", "preserve"}


def default_disk_form() -> str:
    value = os.environ.get("AGENTREMOTE_FILENAME_NORMALIZATION", DEFAULT_WIRE_FORM)
    value = value.upper() if value.lower() != "preserve" else "preserve"
    return value if value in VALID_FORMS else DEFAULT_WIRE_FORM


def filename_policy() -> FilenamePolicy:
    return FilenamePolicy(
        wire_form=DEFAULT_WIRE_FORM,
        disk_form=default_disk_form(),
        local_os=platform.system() or "unknown",
    )


def normalize_text(value: str, form: str | None = None) -> str:
    chosen = form or DEFAULT_WIRE_FORM
    if chosen == "preserve":
        return value
    return unicodedata.normalize(chosen, value)


def normalize_wire(value: str) -> str:
    return normalize_text(value, DEFAULT_WIRE_FORM)


def normalize_disk(value: str, form: str | None = None) -> str:
    return normalize_text(value, form or default_disk_form())


def filename_key(value: str) -> str:
    return normalize_wire(value)


def contains_control(value: str) -> bool:
    return any(unicodedata.category(ch)[0] == "C" for ch in value)


def normalization_info(value: str) -> dict[str, bool]:
    return {
        "nfc": unicodedata.is_normalized("NFC", value),
        "nfd": unicodedata.is_normalized("NFD", value),
        "nfkc": unicodedata.is_normalized("NFKC", value),
        "nfkd": unicodedata.is_normalized("NFKD", value),
    }

