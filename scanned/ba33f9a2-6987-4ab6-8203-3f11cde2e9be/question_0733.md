# Q733: Low core cache invalidation failure in is_dirty

## Question
Can an unprivileged attacker use a script or network payload causing production code to parse, convert, or cache attacker-shaped data to alternate valid and invalid conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads so `is_dirty` in `util/build-info/src/lib.rs` leaves a cache, index, or status flag stale and trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/build-info/src/lib.rs::is_dirty`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
