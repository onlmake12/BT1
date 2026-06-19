# Q2349: Low rpc replay reorder race in GetBlocksProofProcess

## Question
Can an unprivileged attacker replay, reorder, or delay RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `GetBlocksProofProcess` in `util/light-client-protocol-server/src/components/get_blocks_proof.rs` takes a stale branch and make RPC/indexer code panic or allocate heavily before validation clamps the request, breaking the invariant that RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_blocks_proof.rs::GetBlocksProofProcess`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
