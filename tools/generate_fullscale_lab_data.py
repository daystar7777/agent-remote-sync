from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import unicodedata
from pathlib import Path


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_unicode_dataset(root: Path) -> None:
    unicode_dir = root / "DS-unicode"
    unicode_dir.mkdir(parents=True, exist_ok=True)
    names = [
        "카페-한글.txt",
        unicodedata.normalize("NFD", "cafe-é.txt"),
        unicodedata.normalize("NFC", "cafe-é.txt"),
        "日本語-テスト.txt",
    ]
    manifest: list[dict[str, object]] = []
    seen_wire_names: set[str] = set()
    for index, name in enumerate(names):
        wire_name = unicodedata.normalize("NFC", name)
        collision = wire_name in seen_wire_names
        seen_wire_names.add(wire_name)
        actual_name = name if not collision else f"collision-{index}-{wire_name}"
        actual = unicode_dir / actual_name
        write_text(actual, f"name={name}\nactual={actual.name}\n")
        manifest.append(
            {
                "requested": name,
                "actual": actual.name,
                "collision": collision,
                "nfc": unicodedata.normalize("NFC", name),
                "nfd": unicodedata.normalize("NFD", name),
                "codepoints": [f"U+{ord(ch):04X}" for ch in name],
            }
        )
    (unicode_dir / "unicode-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_many_dataset(root: Path, count: int) -> None:
    many = root / "DS-many"
    many.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        folder = many / f"group-{index // 250:02d}"
        write_text(folder / f"item-{index:05d}.txt", f"item {index}\n")


def write_large_dataset(root: Path, size_mib: int) -> None:
    large = root / "DS-large"
    large.mkdir(parents=True, exist_ok=True)
    large_file = large / f"large-{size_mib}mib.bin"
    chunk = hashlib.sha256(b"agent-remote-sync-v01").digest() * 32768
    remaining = max(0, size_mib) * 1024 * 1024
    if remaining and not large_file.exists():
        with large_file.open("wb") as handle:
            while remaining:
                part = chunk[: min(len(chunk), remaining)]
                handle.write(part)
                remaining -= len(part)
    if large_file.exists():
        digest = hashlib.sha256()
        with large_file.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        (large / f"{large_file.name}.sha256").write_text(
            digest.hexdigest() + "\n",
            encoding="utf-8",
        )


def generate(root: Path, many_count: int, large_size_mib: int) -> None:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("DS-small", "DS-empty", "DS-conflict", "DS-unicode", "DS-many", "DS-large"):
        target = root / name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

    small = root / "DS-small"
    for index in range(5):
        write_text(small / f"file-{index}.txt", f"small {index}\n")
    write_text(small / "folder-a" / "nested.txt", "nested\n")
    (small / "folder-b").mkdir(parents=True, exist_ok=True)

    empty = root / "DS-empty"
    (empty / "empty-folder").mkdir(parents=True, exist_ok=True)
    write_text(empty / "empty-file.txt", "")

    conflict = root / "DS-conflict"
    write_text(conflict / "same-path.txt", "local conflict candidate\n")

    write_unicode_dataset(root)
    write_many_dataset(root, many_count)
    write_large_dataset(root, large_size_mib)

    print(root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate agentremote full-scale lab data")
    parser.add_argument("--root", required=True, help="output root for generated datasets")
    parser.add_argument("--many-count", type=int, default=5000, help="number of DS-many files")
    parser.add_argument(
        "--large-size-mib",
        type=int,
        default=1024,
        help="large file size in MiB; use 0 to skip",
    )
    args = parser.parse_args()
    generate(Path(args.root), max(0, args.many_count), max(0, args.large_size_mib))


if __name__ == "__main__":
    main()
