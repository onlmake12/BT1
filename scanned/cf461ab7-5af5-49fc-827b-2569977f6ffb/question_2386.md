# Q2386: High rpc resource amplification in LightClientProtocolReply

## Question
Can an unprivileged attacker repeatedly send small block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to make `LightClientProtocolReply` in `util/light-client-protocol-server/src/prelude.rs` amplify CPU, memory, storage, or bandwidth and amplify storage scans or proof generation with small crafted RPC requests, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/light-client-protocol-server/src/prelude.rs::LightClientProtocolReply`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
