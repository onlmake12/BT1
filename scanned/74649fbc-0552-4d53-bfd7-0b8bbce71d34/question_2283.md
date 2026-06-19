# Q2283: Low rpc cross module inconsistency in expecting

## Question
Can an unprivileged attacker use an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to make `expecting` in `util/jsonrpc-types/src/fixed_bytes.rs` return a result that downstream modules interpret differently, where cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/fixed_bytes.rs::expecting`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
