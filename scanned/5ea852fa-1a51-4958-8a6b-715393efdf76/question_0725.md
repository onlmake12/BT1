# Q725: Low core limit off by one in From

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for local config or RPC parameters that flow into production node behavior through a local operator invoking a default-enabled node path that depends on this module so `From` in `error/src/util.rs` trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `error/src/util.rs::From`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: local config or RPC parameters that flow into production node behavior
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
