# Q676: Critical consensus limit off by one in is_relative

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a sync peer delivering reordered headers, uncles, and block extensions so `is_relative` in `verification/src/transaction_verifier.rs` force two verification paths to classify the same block differently around a boundary check, violating all honest nodes must deterministically accept and reject the same blocks under the same consensus spec, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `verification/src/transaction_verifier.rs::is_relative`
- Entrypoint: a sync peer delivering reordered headers, uncles, and block extensions
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: all honest nodes must deterministically accept and reject the same blocks under the same consensus spec
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
