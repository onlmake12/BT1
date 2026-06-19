# Q533: Critical consensus restart reorg persistence in complete_mainnet

## Question
Can an unprivileged attacker shape uncle lists, proposal IDs, block extension bytes, transaction roots, and DAO fields through a sync peer delivering reordered headers, uncles, and block extensions, then force normal restart, reorg, retry, or replay handling so `complete_mainnet` in `spec/src/hardfork.rs` persists inconsistent state and force two verification paths to classify the same block differently around a boundary check, violating all honest nodes must deterministically accept and reject the same blocks under the same consensus spec, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `spec/src/hardfork.rs::complete_mainnet`
- Entrypoint: a sync peer delivering reordered headers, uncles, and block extensions
- Attacker controls: uncle lists, proposal IDs, block extension bytes, transaction roots, and DAO fields
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: all honest nodes must deterministically accept and reject the same blocks under the same consensus spec
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
