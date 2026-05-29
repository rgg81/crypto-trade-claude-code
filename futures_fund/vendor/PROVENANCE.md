# Vendored analytical scripts

Copied **verbatim** on 2026-05-29 from the user's personal Claude Code skills so the
futures-fund repo is self-contained and reproducible (spec §11 — project-only, all committed).

| File | Upstream source |
|---|---|
| `regime_detection.py` | `~/.claude/skills/regime-detection/scripts/detect_regime.py` |
| `feature_engineering.py` | `~/.claude/skills/feature-engineering/scripts/build_features.py` |
| `walk_forward.py` | `~/.claude/skills/walk-forward-validation/scripts/walk_forward.py` |
| `overfit_detector.py` | `~/.claude/skills/walk-forward-validation/scripts/overfit_detector.py` |

**Do not hand-edit** beyond import hygiene. To update, re-copy from upstream and re-run the smoke tests.
The Solana/Birdeye data-fetch helpers inside these files are unused here (A2 has its own Binance client);
we use only the pure compute functions (indicators, regime classification, features, walk-forward, DSR/PBO).
