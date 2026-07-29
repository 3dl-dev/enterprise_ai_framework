# Deferred work

**Deferred work lives in `rd`, not here.**

This file existed because `rd` accepted writes in this repo and could not read them back —
every item created on 2026-07-27 was invisible to `rd show` and `rd list`. That is fixed;
the whole tree reads back. The one item this file held, local-model cost accounting plus
SkyPilot, is `enterpriseaiframework-226`, and its full reasoning, constraints and done
condition now live on the item rather than in a document beside it.

```bash
rd show enterpriseaiframework-226   # local-model cost accounting + SkyPilot
rd ready                            # everything actionable
```

Do not restart this file. A second place to record work is a second place to go stale, and
this one did: it also carried an "operator price-override file" as a cheap adjacent win,
which finding 31 later showed rested on a false premise about unpriced models metering at
zero.
