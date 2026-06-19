# Q903: Low core resource amplification in calc_proposals_hash

## Question
Can an unprivileged attacker repeatedly send small conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a script or network payload causing production code to parse, convert, or cache attacker-shaped data to make `calc_proposals_hash` in `util/gen-types/src/extension/calc_hash.rs` amplify CPU, memory, storage, or bandwidth and make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/gen-types/src/extension/calc_hash.rs::calc_proposals_hash`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make a shared helper produce different results for consensus, RPC, storage, and tx-pool callers
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
