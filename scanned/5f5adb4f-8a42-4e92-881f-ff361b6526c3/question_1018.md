# Q1018: High core limit off by one in shrink_to_fit

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads through a script or network payload causing production code to parse, convert, or cache attacker-shaped data so `shrink_to_fit` in `util/src/shrink_to_fit.rs` break a resource bound or state transition that downstream modules assume is already enforced, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/src/shrink_to_fit.rs::shrink_to_fit`
- Entrypoint: a script or network payload causing production code to parse, convert, or cache attacker-shaped data
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
