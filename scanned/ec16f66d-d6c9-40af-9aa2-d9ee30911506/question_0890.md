# Q890: Low core batch interaction bug in TryFrom

## Question
Can an unprivileged attacker batch conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths so `TryFrom` in `util/gen-types/src/core.rs` handles the first item safely but applies incorrect assumptions to later items and make canonical serialization or conversion accept an ambiguous representation, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/gen-types/src/core.rs::TryFrom`
- Entrypoint: an unprivileged peer, RPC caller, or transaction sender reaching this shared module through production paths
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
