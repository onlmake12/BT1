# Q2296: Low rpc restart reorg persistence in DeploymentState

## Question
Can an unprivileged attacker shape RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a light-client protocol caller requesting proofs and filters across reorg boundaries, then force normal restart, reorg, retry, or replay handling so `DeploymentState` in `util/jsonrpc-types/src/info.rs` persists inconsistent state and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/info.rs::DeploymentState`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
