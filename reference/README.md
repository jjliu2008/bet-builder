# reference/

Prior research code kept for reuse, **not** imported by the engine.

Planned contents: `surface.py` (Deribit SVI surface: `fit_svi`, `svi`, `black_call`) and
`xv_snap.py` (Gemini/Kalshi cross-venue snapshot + contract matching).
These two files were not on the engine lead's machine when the repo was bootstrapped, so they
still need to be dropped in here. The engine re-implements the needed logic independently:
SVI fit and Black pricing in `engine/fairvalue.py`, venue matching in `engine/dev_fetch.py`.
