# Q1021: High core limit off by one in shrink_to_fit

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a local operator invoking a default-enabled node path that depends on this module so `shrink_to_fit` in `util/src/shrink_to_fit.rs` trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/src/shrink_to_fit.rs::shrink_to_fit`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: shared production helpers must be deterministic, bounded, and canonical for all security-relevant callers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
