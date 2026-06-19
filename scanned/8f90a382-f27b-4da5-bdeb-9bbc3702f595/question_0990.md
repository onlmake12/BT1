# Q990: High core parser precheck gap in get_uptime

## Question
Can an unprivileged attacker submit malformed-but-reachable conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a script or network payload causing production code to parse, convert, or cache attacker-shaped data so `get_uptime` in `util/onion/src/tor_controller.rs` performs expensive or unsafe work before validation and trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input, violating module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/onion/src/tor_controller.rs::get_uptime`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: trigger panic, overflow, stale cache, or excessive work before caller-level validation handles the input
- Invariant to test: module-level assumptions must hold across consensus, network, RPC, storage, and tx-pool paths
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
