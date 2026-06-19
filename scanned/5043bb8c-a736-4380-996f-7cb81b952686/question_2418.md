# Q2418: Low rpc limit off by one in RichIndexerHandle

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a light-client protocol caller requesting proofs and filters across reorg boundaries so `RichIndexerHandle` in `util/rich-indexer/src/indexer_handle/mod.rs` return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/mod.rs::RichIndexerHandle`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
