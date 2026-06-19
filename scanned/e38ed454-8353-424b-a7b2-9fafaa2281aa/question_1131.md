# Q1131: Low core differential path split in compact_to_difficulty

## Question
Can an unprivileged attacker reach `compact_to_difficulty` in `util/types/src/utilities/difficulty.rs` through two production paths from an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths and make one path accept while the other rejects because of conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/types/src/utilities/difficulty.rs::compact_to_difficulty`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
