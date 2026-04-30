from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FilenamePolicy:
    wire_form: str
    disk_form: str
    local_os: str

