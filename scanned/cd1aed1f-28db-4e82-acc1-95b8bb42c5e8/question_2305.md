# Q2305: Low rpc restart reorg persistence in ResponseFormatInnerType

## Question
Can an unprivileged attacker shape indexer state freshness, reorg timing, block-filter requests, and proof target positions through a local RPC caller invoking public JSON-RPC methods with crafted parameters, then force normal restart, reorg, retry, or replay handling so `ResponseFormatInnerType` in `util/jsonrpc-types/src/lib.rs` persists inconsistent state and amplify storage scans or proof generation with small crafted RPC requests, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/lib.rs::ResponseFormatInnerType`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
