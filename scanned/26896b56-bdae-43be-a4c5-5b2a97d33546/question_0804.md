# Q804: High core boundary divergence in mainnet

## Question
Can an unprivileged attacker enter through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths and use local config or RPC parameters that flow into production node behavior to drive `mainnet` in `util/constant/src/softfork/mainnet.rs` across a boundary where trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating the invariant that shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/constant/src/softfork/mainnet.rs::mainnet`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
