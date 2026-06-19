# Q1104: High core boundary divergence in global

## Question
Can an unprivileged attacker enter through a script or network payload causing production code to parse, convert, or cache attacker-shaped data and use message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs to drive `global` in `util/types/src/global.rs` across a boundary where make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating the invariant that security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/types/src/global.rs::global`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
