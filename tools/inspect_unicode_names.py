from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path


def safe_print(value: object = "") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    sys.stdout.write("\n")


def describe(path: Path) -> str:
    name = path.name
    codepoints = " ".join(f"U+{ord(ch):04X}" for ch in name)
    return (
        f"raw={name!r}\n"
        f"  nfc={unicodedata.normalize('NFC', name)!r}\n"
        f"  nfd={unicodedata.normalize('NFD', name)!r}\n"
        f"  codepoints={codepoints}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Unicode filenames for cross-OS validation")
    parser.add_argument("root", help="root folder to inspect")
    args = parser.parse_args()
    root = Path(args.root)
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        if path.is_file():
            safe_print(path.relative_to(root))
            safe_print(describe(path))


if __name__ == "__main__":
    main()
