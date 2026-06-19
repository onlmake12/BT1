# Q891: Low core resource amplification in TryFrom

## Question
Can an unprivileged attacker repeatedly send small conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a script or network payload causing production code to parse, convert, or cache attacker-shaped data to make `TryFrom` in `util/gen-types/src/core.rs` amplify CPU, memory, storage, or bandwidth and trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/gen-types/src/core.rs::TryFrom`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
