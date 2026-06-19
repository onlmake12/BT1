# Q787: High core limit off by one in latest_assume_valid_target

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for local config or RPC parameters that flow into production node behavior through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `latest_assume_valid_target` in `util/constant/src/latest_assume_valid_target.rs` trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/constant/src/latest_assume_valid_target.rs::latest_assume_valid_target`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
