# Q977: High core boundary divergence in OnionServiceConfig

## Question
Can an unprivileged attacker enter through a local operator invoking a default-enabled node path that depends on this module and use message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs to drive `OnionServiceConfig` in `util/onion/src/lib.rs` across a boundary where break a resource bound or state transition that downstream modules assume is already enforced, violating the invariant that shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/onion/src/lib.rs::OnionServiceConfig`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: message order, retry timing, reorg state, cache pressure, and malformed but well-typed inputs
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
