# Q820: High core differential path split in testnet

## Question
Can an unprivileged attacker reach `testnet` in `util/constant/src/softfork/testnet.rs` through two production paths from an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths and make one path accept while the other rejects because of local config or RPC parameters that flow into production node behavior, violating shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/constant/src/softfork/testnet.rs::testnet`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
