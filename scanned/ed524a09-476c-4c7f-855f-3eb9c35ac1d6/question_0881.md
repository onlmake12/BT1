# Q881: High core canonical encoding ambiguity in is_utf8

## Question
Can an unprivileged attacker craft alternate encodings for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a block or transaction relayer triggering this helper during validation, sync, or storage updates so `is_utf8` in `util/gen-types/src/conversion/primitive.rs` accepts two representations for one security object and make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/gen-types/src/conversion/primitive.rs::is_utf8`
- Entrypoint: a block or transaction relayer triggering this helper during validation, sync, or storage updates
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
