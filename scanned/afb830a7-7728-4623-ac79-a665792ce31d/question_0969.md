# Q969: High core limit off by one in should_be_ok

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `should_be_ok` in `util/gen-types/src/prelude.rs` make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/gen-types/src/prelude.rs::should_be_ok`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
