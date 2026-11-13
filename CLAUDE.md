# Notes for Claude

- Never read, print or ask for `.env`, keychain values or any API key. Check secrets only with `trading-agent secrets status`.
- Only `paper` mode is allowed. Do not add live keys, live URLs or a live mode before the Phase 8 go-live gate.
- Hard risk limits belong in deterministic code, never in prompts.
- Every signal, proposal, decision and order gets an audit-log event (`storage.record_event`). The audit log is append-only.
- Run `pytest` after changes. Tests must never touch the real keychain (see `tests/conftest.py`).
