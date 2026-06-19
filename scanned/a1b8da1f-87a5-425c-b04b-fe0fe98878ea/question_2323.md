# Q2323: Low rpc boundary divergence in epoch_number

## Question
Can an unprivileged attacker enter through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and use block/template parameters, transaction payloads, fee-rate values, and debug/experiment options to drive `epoch_number` in `util/jsonrpc-types/src/primitive.rs` across a boundary where make RPC/indexer code panic or allocate heavily before validation clamps the request, violating the invariant that RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/primitive.rs::epoch_number`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
