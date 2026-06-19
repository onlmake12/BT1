# Q973: Low core boundary divergence in OnionServiceConfig

## Question
Can an unprivileged attacker enter through a local operator invoking a default-enabled node path that depends on this module and use conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads to drive `OnionServiceConfig` in `util/onion/src/lib.rs` across a boundary where trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating the invariant that module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/onion/src/lib.rs::OnionServiceConfig`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
