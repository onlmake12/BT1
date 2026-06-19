# Q1046: High core parser precheck gap in from

## Question
Can an unprivileged attacker submit malformed-but-reachable local config or RPC parameters that flow into production node behavior through a script or network payload causing production code to parse, convert, or cache attacker-shaped data so `from` in `util/types/src/block_number_and_hash.rs` performs expensive or unsafe work before validation and make canonical serialization or conversion accept an ambiguous representation, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/types/src/block_number_and_hash.rs::from`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
