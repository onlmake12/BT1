# Q987: Low core state transition mismatch in get_bootstrap_phase

## Question
Can an unprivileged attacker enter through a script or network payload causing production code to parse, convert, or cache attacker-shaped data and sequence conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads so `get_bootstrap_phase` in `util/onion/src/tor_controller.rs` observes pre-state and post-state from different views, letting the flow make canonical serialization or conversion accept an ambiguous representation, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/onion/src/tor_controller.rs::get_bootstrap_phase`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
