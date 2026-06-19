# Q2245: Low rpc resource amplification in From

## Question
Can an unprivileged attacker repeatedly send small RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a light-client protocol caller requesting proofs and filters across reorg boundaries to make `From` in `util/jsonrpc-types/src/block_template.rs` amplify CPU, memory, storage, or bandwidth and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/block_template.rs::From`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
