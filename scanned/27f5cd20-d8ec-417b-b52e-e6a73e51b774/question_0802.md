# Q802: High core cross module inconsistency in mainnet

## Question
Can an unprivileged attacker use a local operator invoking a default-enabled node path that depends on this module to make `mainnet` in `util/constant/src/softfork/mainnet.rs` return a result that downstream modules interpret differently, where make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/constant/src/softfork/mainnet.rs::mainnet`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
