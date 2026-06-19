# Q2369: Low rpc limit off by one in components

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for indexer state freshness, reorg timing, block-filter requests, and proof target positions through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `components` in `util/light-client-protocol-server/src/components/mod.rs` amplify storage scans or proof generation with small crafted RPC requests, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/components/mod.rs::components`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
