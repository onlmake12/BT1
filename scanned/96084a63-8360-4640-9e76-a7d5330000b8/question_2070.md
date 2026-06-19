# Q2070: High rpc batch interaction bug in build_filter_data

## Question
Can an unprivileged attacker batch JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `build_filter_data` in `block-filter/src/filter.rs` handles the first item safely but applies incorrect assumptions to later items and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `block-filter/src/filter.rs::build_filter_data`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
