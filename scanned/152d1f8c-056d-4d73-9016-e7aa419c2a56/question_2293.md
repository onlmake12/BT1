# Q2293: Low rpc differential path split in tx_hash

## Question
Can an unprivileged attacker reach `tx_hash` in `util/jsonrpc-types/src/indexer.rs` through two production paths from a light-client protocol caller requesting proofs and filters across reorg boundaries and make one path accept while the other rejects because of block/template parameters, transaction payloads, fee-rate values, and debug/experiment options, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/indexer.rs::tx_hash`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
