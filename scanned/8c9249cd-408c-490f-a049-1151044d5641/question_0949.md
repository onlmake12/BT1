# Q949: Low core differential path split in from_witness

## Question
Can an unprivileged attacker reach `from_witness` in `util/gen-types/src/extension/shortcut.rs` through two production paths from a block or transaction relayer triggering this helper during validation, sync, or storage updates and make one path accept while the other rejects because of conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads, violating caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/gen-types/src/extension/shortcut.rs::from_witness`
- Entrypoint: a block or transaction relayer triggering this helper during validation, sync, or storage updates
- Attacker controls: conversion inputs, collection lengths, duplicate identifiers, empty values, and maximum-size payloads
- Exploit idea: break a resource bound or state transition that downstream modules assume is already enforced
- Invariant to test: caller-controlled input must not cause panic, ambiguous conversion, stale state, or unbounded work
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Create a minimal caller harness for the shared module and compare outputs across boundary inputs, serialization forms, and repeated calls.
