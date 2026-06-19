# Q2094: Low rpc restart reorg persistence in AlertRpc

## Question
Can an unprivileged attacker shape RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a light-client protocol caller requesting proofs and filters across reorg boundaries, then force normal restart, reorg, retry, or replay handling so `AlertRpc` in `rpc/src/module/alert.rs` persists inconsistent state and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/alert.rs::AlertRpc`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
