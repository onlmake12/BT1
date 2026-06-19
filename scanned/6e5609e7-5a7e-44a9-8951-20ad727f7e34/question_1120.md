# Q1120: High core parser precheck gap in reset_header

## Question
Can an unprivileged attacker submit malformed-but-reachable serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values through a block or transaction relayer triggering this helper during validation, sync, or storage updates so `reset_header` in `util/types/src/prelude.rs` performs expensive or unsafe work before validation and break a resource bound or state transition that downstream modules assume is already enforced, violating shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/types/src/prelude.rs::reset_header`
- Entrypoint: a block or transaction relayer triggering this helper during validation, sync, or storage updates
- Attacker controls: serialized CKB objects, hashes, indexes, ranges, counts, options, and boundary numeric values
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
