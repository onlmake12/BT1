# Q979: High core state transition mismatch in create_tor_secret_key

## Question
Can an unprivileged attacker enter through a local operator invoking a default-enabled node path that depends on this module and sequence conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads so `create_tor_secret_key` in `util/onion/src/onion_service.rs` observes pre-state and post-state from different views, letting the flow make canonical serialization or conversion accept an ambiguous representation, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/onion/src/onion_service.rs::create_tor_secret_key`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
