---
inclusion: always
---

# Product

**tgobjects3** is an S3-compatible object storage gateway that uses private Telegram channels as the durable storage backend.

## What it does

- Exposes an S3-compatible HTTP API (SigV4 auth) so standard S3 clients can put, get, list, and delete objects.
- Stores object data as encrypted record chunks uploaded as documents into per-bucket Telegram channels.
- Maps each bucket to exactly one private Telegram channel; objects are versioned by generation.
- Keeps a PostgreSQL index as a *cache* of state. The authoritative, durable state lives in Telegram and the index can be rebuilt from channel contents by the recovery CLI.

## Core guarantees & concepts

- **Durability lives in Telegram, not the database.** Treat PostgreSQL as a rebuildable index/cache, never the source of truth. Never introduce logic that assumes the database holds authoritative state.
- **Encryption at rest.** A root master key wraps per-bucket KEKs (key-encryption keys); record payloads are encrypted before upload. Never log, persist, or transmit unwrapped keys or plaintext payloads outside the encryption boundary.
- **Generations & commits.** Object versions are committed as records; checkpoints and tombstones support recovery and garbage collection.
- **Single MTProto session.** All Telegram access goes through one session behind a typed port interface — never call Telethon directly outside the Telegram adapter.

## Components

- **gateway** — the running service (S3 API + admin API + storage / telegram / cache / jobs machinery).
- **cli** — rebuild/recovery tooling and benchmark harness.
- **console** — React + Vite admin SPA (currently a stub).
- **deploy** — docker-compose / Dockerfile / nginx (currently a stub).

## Rules for changes

- Preserve S3 API semantics and SigV4 compatibility; standard S3 clients must keep working.
- Any change to on-wire/on-disk record, checkpoint, or tombstone formats must stay rebuildable by the recovery CLI.
- When a feature touches durable state, implement it against Telegram as the source of truth and treat the index as derived.
