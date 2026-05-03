# Filename Normalization

Windows, macOS, and Linux can represent the same visible filename with different
Unicode byte sequences. This is especially painful for Korean and accented
characters when files move between Windows and macOS.

agent-remote-sync treats filenames as Unicode text and normalizes protocol paths to NFC.

## Policy

- Wire/API paths: NFC.
- Directory listings: names and paths are returned as NFC.
- Path lookup: an NFC request can resolve an existing NFD filename on disk.
- New files and folders: created as NFC by default.
- Partial transfer state: keyed by normalized path, so resume works across
  normalization variants.

This means a file that exists on macOS as decomposed NFD can be requested as the
normal composed name from Windows, and a received file will normally be written
with the composed NFC name.

## Configuration

The default disk write form is NFC. It can be overridden for special cases:

```powershell
$env:AGENTREMOTE_FILENAME_NORMALIZATION = "NFC"
$env:AGENTREMOTE_FILENAME_NORMALIZATION = "NFD"
$env:AGENTREMOTE_FILENAME_NORMALIZATION = "preserve"
```

`NFC` is recommended for cross-platform projects. `preserve` should only be used
when a project has a strong reason to keep exact incoming normalization.

## Ambiguous Names

If a directory contains two names that differ only by Unicode normalization,
agent-remote-sync rejects the lookup as ambiguous. This prevents accidentally reading,
overwriting, or deleting the wrong file.

## Limits

- agent-remote-sync does not repair existing local folders in place.
- It does not solve case-sensitivity differences between filesystems.
- It does not transliterate filenames; it preserves characters and normalizes
  Unicode representation only.

