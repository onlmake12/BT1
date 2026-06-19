# Q983: Low core parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a local operator invoking a default-enabled node path that depends on this module so `new` in `util/onion/src/onion_service.rs` performs expensive or unsafe work before validation and make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/onion/src/onion_service.rs::new`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
