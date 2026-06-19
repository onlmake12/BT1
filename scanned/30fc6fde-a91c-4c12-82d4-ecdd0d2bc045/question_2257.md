# Q2257: Low rpc limit off by one in ExtraLoggerConfig

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `ExtraLoggerConfig` in `util/jsonrpc-types/src/debug.rs` amplify storage scans or proof generation with small crafted RPC requests, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/debug.rs::ExtraLoggerConfig`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
