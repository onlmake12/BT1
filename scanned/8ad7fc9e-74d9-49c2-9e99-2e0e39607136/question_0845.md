# Q845: High core canonical encoding ambiguity in from

## Question
Can an unprivileged attacker craft alternate encodings for message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `from` in `util/gen-types/src/conversion/blockchain/mod.rs` accepts two representations for one security object and break a resource bound or state transition that downstream modules assume is already enforced, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/gen-types/src/conversion/blockchain/mod.rs::from`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
