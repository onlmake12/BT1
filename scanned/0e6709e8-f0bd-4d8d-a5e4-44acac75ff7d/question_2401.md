# Q2401: Low rpc limit off by one in bulk_insert_blocks_simple

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a light-client protocol caller requesting proofs and filters across reorg boundaries so `bulk_insert_blocks_simple` in `util/rich-indexer/src/indexer/insert.rs` return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer/insert.rs::bulk_insert_blocks_simple`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
