# Q817: High core resource amplification in softfork

## Question
Can an unprivileged attacker repeatedly send small conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths to make `softfork` in `util/constant/src/softfork/mod.rs` amplify CPU, memory, storage, or bandwidth and make canonical serialization or conversion accept an ambiguous representation, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/constant/src/softfork/mod.rs::softfork`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
