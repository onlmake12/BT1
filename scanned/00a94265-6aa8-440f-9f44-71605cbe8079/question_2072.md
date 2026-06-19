# Q2072: High rpc batch interaction bug in build_filter_data

## Question
Can an unprivileged attacker batch JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `build_filter_data` in `block-filter/src/filter.rs` handles the first item safely but applies incorrect assumptions to later items and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `block-filter/src/filter.rs::build_filter_data`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
