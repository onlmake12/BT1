# Q2372: High rpc replay reorder race in constant

## Question
Can an unprivileged attacker replay, reorder, or delay indexer state freshness, reorg timing, block-filter requests, and proof target positions through a light-client protocol caller requesting proofs and filters across reorg boundaries so `constant` in `util/light-client-protocol-server/src/constant.rs` takes a stale branch and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, breaking the invariant that RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/light-client-protocol-server/src/constant.rs::constant`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
