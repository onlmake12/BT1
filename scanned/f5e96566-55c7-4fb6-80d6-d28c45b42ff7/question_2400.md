# Q2400: Low rpc batch interaction bug in bulk_insert_block_association_uncle_table

## Question
Can an unprivileged attacker batch indexer state freshness, reorg timing, block-filter requests, and proof target positions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `bulk_insert_block_association_uncle_table` in `util/rich-indexer/src/indexer/insert.rs` handles the first item safely but applies incorrect assumptions to later items and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer/insert.rs::bulk_insert_block_association_uncle_table`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
