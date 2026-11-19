# Source provenance

Ported from https://github.com/Vishalp52/AEGIS, local clean checkout at
`3bae9fb802fe607b6d26e9ce1c7a907598250e62`.

- `fusion.py`: exact decimal arithmetic, score validation, deterministic fusion.
  Removed `compute_fused_risk`, which depends on wallet-specific Gemini schemas.
- `policy.py`: validated thresholds and decision bands. Removed wallet-access
  evaluation, customer messages and wallet reason-code imports.

The market adapter uses only
`deterministic_score` and `band`; account-access messages are never surfaced.
No wallet model, chain calls, semantic triage, threat database or credentials are imported.
