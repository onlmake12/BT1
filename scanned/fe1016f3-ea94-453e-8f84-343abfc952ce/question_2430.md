# Q2430: High rpc differential path split in index_tx_pool

## Question
Can an unprivileged attacker reach `index_tx_pool` in `util/rich-indexer/src/service.rs` through two production paths from a light-client protocol caller requesting proofs and filters across reorg boundaries and make one path accept while the other rejects because of RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/service.rs::index_tx_pool`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
