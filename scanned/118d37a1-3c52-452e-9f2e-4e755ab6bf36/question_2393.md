# Q2393: Low rpc boundary divergence in eq

## Question
Can an unprivileged attacker enter through a light-client protocol caller requesting proofs and filters across reorg boundaries and use indexer state freshness, reorg timing, block-filter requests, and proof target positions to drive `eq` in `util/light-client-protocol-server/src/status.rs` across a boundary where cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating the invariant that local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/status.rs::eq`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
