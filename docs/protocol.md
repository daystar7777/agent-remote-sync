# agentFTP Protocol

agentFTP uses a small JSON-over-HTTP API. All paths are root-relative POSIX-like
paths such as `/`, `/src`, or `/src/main.py`, regardless of host OS.

## Authentication

1. `GET /api/challenge`
2. Client derives a PBKDF2-HMAC-SHA256 key from the password and returned salt.
3. Client sends `POST /api/login` with an HMAC proof for the one-time nonce.
4. Server returns a bearer token and granted scopes.
5. Later requests use `Authorization: Bearer <token>`.

This prevents sending the password directly, but it does not encrypt file
contents. Use a trusted network, VPN, or TLS for untrusted networks.

`POST /api/login` can include `scopes`, either as a list or comma-separated
string. Supported scopes are `read`, `write`, `delete`, and `handoff`. If scopes
are omitted, the server grants all scopes.

## Core Endpoints

- `GET /api/list?path=/`
- `GET /api/stat?path=/file.txt`
- `GET /api/tree?path=/folder`
- `GET /api/storage`
- `POST /api/plan/upload`
- `POST /api/plan/download`
- `POST /api/mkdir`
- `POST /api/delete`
- `POST /api/rename`
- `POST /api/move`
- `POST /api/jobs/<id>/cancel` on the local master UI server

`GET /api/storage` reports disk usage for the exposed root filesystem:

```json
{
  "path": "/shared/root",
  "totalBytes": 1000000000,
  "usedBytes": 250000000,
  "freeBytes": 750000000,
  "freeRatio": 0.75
}
```

`POST /api/plan/upload` and `POST /api/plan/download` return a reusable
transfer plan for the master UI. The plan includes file and directory counts,
conflicts, remaining bytes after partial files, destination storage, warnings,
and a `planId` that can be passed to `/api/jobs/upload` or
`/api/jobs/download`.

## Resumable Download

`GET /api/download?path=/big.bin&offset=0&length=8388608`

The server returns bytes from the requested range. The client writes them to a
partial file and resumes from the current partial size after interruption.

## Resumable Upload

1. `POST /api/upload/status`
2. `PUT /api/upload/chunk?path=/target.bin&offset=<n>&total=<size>`
3. Repeat chunks sequentially.
4. `POST /api/upload/finish`

The receiver stores data under `.agentftp_partial` until completion, then moves
the file into place atomically where possible.

If a chunk was written but the response was lost, a repeated chunk may receive
`offset_mismatch` with the already-received offset. The master treats the exact
"previous chunk landed" case as successful progress. `POST /api/upload/finish`
is idempotent when the final target already exists with the expected size and
SHA-256 hash.
