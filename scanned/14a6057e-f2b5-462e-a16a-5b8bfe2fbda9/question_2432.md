# Q2432: Low rpc batch interaction bug in spawn_poll

## Question
Can an unprivileged attacker batch indexer state freshness, reorg timing, block-filter requests, and proof target positions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `spawn_poll` in `util/rich-indexer/src/service.rs` handles the first item safely but applies incorrect assumptions to later items and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/service.rs::spawn_poll`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
