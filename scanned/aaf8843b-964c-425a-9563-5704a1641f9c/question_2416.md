# Q2416: Low rpc replay reorder race in test_get_binary_upper_boundary_leading_zeros

## Question
Can an unprivileged attacker replay, reorder, or delay RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a light-client protocol caller requesting proofs and filters across reorg boundaries so `test_get_binary_upper_boundary_leading_zeros` in `util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs` takes a stale branch and make RPC/indexer code panic or allocate heavily before validation clamps the request, breaking the invariant that RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs::test_get_binary_upper_boundary_leading_zeros`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
