# Q726: High core boundary divergence in from

## Question
Can an unprivileged attacker enter through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths and use conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads to drive `from` in `error/src/util.rs` across a boundary where make canonical serialization or conversion accept an ambiguous representation, violating the invariant that module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `error/src/util.rs::from`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
