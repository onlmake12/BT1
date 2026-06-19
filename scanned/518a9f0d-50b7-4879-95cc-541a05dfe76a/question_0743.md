# Q743: High core limit off by one in next

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `next` in `util/chain-iter/src/lib.rs` make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/chain-iter/src/lib.rs::next`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
