# Q2128: Low rpc differential path split in get_peers

## Question
Can an unprivileged attacker reach `get_peers` in `rpc/src/module/net.rs` through two production paths from a light-client protocol caller requesting proofs and filters across reorg boundaries and make one path accept while the other rejects because of indexer state freshness, reorg timing, block-filter requests, and proof target positions, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/net.rs::get_peers`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
