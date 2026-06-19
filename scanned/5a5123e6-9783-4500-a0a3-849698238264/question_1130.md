# Q1130: High core canonical encoding ambiguity in compact_to_difficulty

## Question
Can an unprivileged attacker craft alternate encodings for local config or RPC parameters that flow into production node behavior through a block or transaction relayer triggering this helper during validation, sync, or storage updates so `compact_to_difficulty` in `util/types/src/utilities/difficulty.rs` accepts two representations for one security object and break a resource bound or state transition that downstream modules assume is already enforced, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/types/src/utilities/difficulty.rs::compact_to_difficulty`
- Entrypoint: a block or transaction relayer triggering this helper during validation, sync, or storage updates
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
