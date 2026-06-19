# Q1052: Low core limit off by one in constants

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a local operator invoking a default-enabled node path that depends on this module so `constants` in `util/types/src/constants.rs` make canonical serialization or conversion accept an ambiguous representation, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/types/src/constants.rs::constants`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
