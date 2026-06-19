# Q1080: High core limit off by one in unpack

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `unpack` in `util/types/src/conversion/storage.rs` break a resource bound or state transition that downstream modules assume is already enforced, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/types/src/conversion/storage.rs::unpack`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
