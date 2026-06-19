# Q2147: High rpc restart reorg persistence in get_blockchain_info

## Question
Can an unprivileged attacker shape JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a light-client protocol caller requesting proofs and filters across reorg boundaries, then force normal restart, reorg, retry, or replay handling so `get_blockchain_info` in `rpc/src/module/stats.rs` persists inconsistent state and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `rpc/src/module/stats.rs::get_blockchain_info`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
