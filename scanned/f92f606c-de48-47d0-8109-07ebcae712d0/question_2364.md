# Q2364: Low rpc canonical encoding ambiguity in get_first_block_total_difficulty_is_not_less_than

## Question
Can an unprivileged attacker craft alternate encodings for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a light-client protocol caller requesting proofs and filters across reorg boundaries so `get_first_block_total_difficulty_is_not_less_than` in `util/light-client-protocol-server/src/components/get_last_state_proof.rs` accepts two representations for one security object and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_last_state_proof.rs::get_first_block_total_difficulty_is_not_less_than`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
