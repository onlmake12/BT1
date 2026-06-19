# Q1132: High core differential path split in compact_to_target

## Question
Can an unprivileged attacker reach `compact_to_target` in `util/types/src/utilities/difficulty.rs` through two production paths from a local operator invoking a default-enabled node path that depends on this module and make one path accept while the other rejects because of serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/types/src/utilities/difficulty.rs::compact_to_target`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
