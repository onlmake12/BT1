# Q1055: High core canonical encoding ambiguity in constants

## Question
Can an unprivileged attacker craft alternate encodings for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a local operator invoking a default-enabled node path that depends on this module so `constants` in `util/types/src/constants.rs` accepts two representations for one security object and make canonical serialization or conversion accept an ambiguous representation, violating security-relevant data must preserve identity and validity across serialization and conversion layers, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/types/src/constants.rs::constants`
- Entrypoint: a local operator invoking a default-enabled node path that depends on this module
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: make canonical serialization or conversion accept an ambiguous representation
- Invariant to test: security-relevant data must preserve identity and validity across serialization and conversion layers
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
