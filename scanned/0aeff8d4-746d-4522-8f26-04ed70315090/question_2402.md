# Q2402: Low rpc limit off by one in bulk_insert_blocks_simple

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a light-client protocol caller requesting proofs and filters across reorg boundaries so `bulk_insert_blocks_simple` in `util/rich-indexer/src/indexer/insert.rs` cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer/insert.rs::bulk_insert_blocks_simple`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
