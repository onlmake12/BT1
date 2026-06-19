# Q2421: High rpc cross module inconsistency in get_indexer_tip

## Question
Can an unprivileged attacker use a light-client protocol caller requesting proofs and filters across reorg boundaries to make `get_indexer_tip` in `util/rich-indexer/src/indexer_handle/mod.rs` return a result that downstream modules interpret differently, where cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/mod.rs::get_indexer_tip`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
