"""Slice the metrics by any dimension — model, surface, tenant, effort, config.

The report's purpose is to compare how each MODEL, HARNESS CONFIG and SURFACE behaves. All
of that is one call: metrics.build_metrics already groups by an arbitrary key function, so a
slice is just "pick the key". This module names the dimensions and enforces tenant isolation.

Tenant isolation: pass `tenant=` to scope a slice to one tenant's records before grouping —
a tenant-scoped view never contains another tenant's numbers. Grouping BY tenant (the global
operator view) is the intended cross-tenant comparison, one column per tenant; because the
records are content-free aggregates, a column is a count, never another tenant's content.

Dimensions:
  model      the model that produced the turn (the default, the headline comparison)
  surface    chat / ide / terminal
  tenant     the operator's tenant id
  principal  the individual user
  effort     reasoning effort — a confound the SKILL says to filter on, exposed as a slice
  config     the harness config fingerprint. PENDING: the normalizer does not yet stamp a
             config label (capturing it needs the pod's mounted ConfigMaps at ingest time —
             deferred by docs/design/records/behavioral-analytics-sources.md and tracked as
             its own item). The dimension is wired here; it yields no groups until records
             carry `config`, rather than inventing a fingerprint that isn't real.
"""

from __future__ import annotations

from . import metrics

DIMENSIONS = {
    "model": lambda r: r.get("model"),
    "surface": lambda r: r.get("surface"),
    "tenant": lambda r: r.get("tenant"),
    "principal": lambda r: r.get("principal"),
    "effort": lambda r: r.get("effort"),
    "config": lambda r: r.get("config"),  # pending normalizer support — see module docstring
}


def slice_metrics(turns, sessions, dimension, *, tenant=None, min_n=metrics.MIN_N, meta=None):
    """Build metrics grouped by `dimension`, optionally scoped to one `tenant`.

    Raises KeyError for an unknown dimension — a typo should fail loudly, not silently return
    an empty report.
    """
    key = DIMENSIONS[dimension]
    if tenant is not None:
        turns = [t for t in turns if t.get("tenant") == tenant]
        sessions = [s for s in sessions if s.get("tenant") == tenant]
    m = metrics.build_metrics(turns, sessions, key=key, min_n=min_n,
                              meta={**(meta or {}), "dimension": dimension, "tenant": tenant})
    return m


def available_dimensions(turns, *, min_distinct=2):
    """Dimensions worth offering in the UI: those with at least `min_distinct` distinct
    non-null values in the corpus (a dimension with one value makes a one-column report)."""
    out = []
    for name, key in DIMENSIONS.items():
        vals = {key(t) for t in turns}
        vals.discard(None)
        if len(vals) >= min_distinct:
            out.append(name)
    return out
