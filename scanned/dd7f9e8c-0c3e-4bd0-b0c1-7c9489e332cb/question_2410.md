# Q2410: Low rpc replay reorder race in query_tx_id_list_by_block_id

## Question
Can an unprivileged attacker replay, reorder, or delay block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `query_tx_id_list_by_block_id` in `util/rich-indexer/src/indexer/remove.rs` takes a stale branch and return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, breaking the invariant that proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer/remove.rs::query_tx_id_list_by_block_id`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
