# Q2361: High rpc state transition mismatch in complete_headers

## Question
Can an unprivileged attacker enter through a local RPC caller invoking public JSON-RPC methods with crafted parameters and sequence indexer state freshness, reorg timing, block-filter requests, and proof target positions so `complete_headers` in `util/light-client-protocol-server/src/components/get_last_state_proof.rs` observes pre-state and post-state from different views, letting the flow amplify storage scans or proof generation with small crafted RPC requests, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_last_state_proof.rs::complete_headers`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
