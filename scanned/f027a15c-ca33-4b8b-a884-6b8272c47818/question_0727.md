# Q727: High core boundary divergence in from

## Question
Can an unprivileged attacker enter through a local operator invoking a default-enabled node path that depends on this module and use serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values to drive `from` in `error/src/util.rs` across a boundary where make canonical serialization or conversion accept an ambiguous representation, violating the invariant that security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `error/src/util.rs::from`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
