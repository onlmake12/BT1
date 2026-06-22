Looking at the vulnerability class — a function returns important accounting delta values, but in one code path those return values are ignored, leaving global totals stale — I need to find the same pattern in CKB's production code.

Let me trace the exact code path in `pool_map.rs`.