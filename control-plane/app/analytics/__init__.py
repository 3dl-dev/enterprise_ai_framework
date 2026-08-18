"""Behavioral analytics — the coding-vs-orchestration report, in-product.

Turn the DAP practice-kit transcript scrapers into a product feature: measure how each
MODEL, HARNESS CONFIG and SURFACE actually behaves across the platform's own sessions, and
price it from the REAL billed ledger rather than an estimated rate card.

Layers (each its own item; see docs/design/records/behavioral-analytics-sources.md):

  measure   pure per-turn measurement primitives (ported from model-behavior.py)
  schema    the harness-agnostic turn/session record shapes
  opencode  opencode SQLite  -> normalized records   (terminal / ide surfaces)
  librechat LibreChat Mongo  -> normalized records   (chat surface)

The normalizer emits CONTENT-FREE records — counts and labels, never transcript text — so
the analytics respect tenant isolation and keep no 3DL service in any data path.
"""
