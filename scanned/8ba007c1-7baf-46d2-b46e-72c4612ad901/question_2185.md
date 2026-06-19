# Q2185: High rpc restart reorg persistence in clone

## Question
Can an unprivileged attacker shape JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs, then force normal restart, reorg, retry, or replay handling so `clone` in `util/indexer-sync/src/custom_filters.rs` persists inconsistent state and amplify storage scans or proof generation with small crafted RPC requests, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/indexer-sync/src/custom_filters.rs::clone`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
