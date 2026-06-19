# Q2127: High rpc cache invalidation failure in clear_banned_addresses

## Question
Can an unprivileged attacker use a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs to alternate valid and invalid JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions so `clear_banned_addresses` in `rpc/src/module/net.rs` leaves a cache, index, or status flag stale and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/module/net.rs::clear_banned_addresses`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
