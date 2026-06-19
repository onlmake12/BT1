# Q671: Critical consensus batch interaction bug in NonContextualTransactionVerifier

## Question
Can an unprivileged attacker batch genesis/spec fields on a private chain and canonical block metadata during replay through a remote peer relaying a crafted block/header sequence so `NonContextualTransactionVerifier` in `verification/src/transaction_verifier.rs` handles the first item safely but applies incorrect assumptions to later items and make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation, violating all honest nodes must deterministically accept and reject the same blocks under the same consensus spec, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `verification/src/transaction_verifier.rs::NonContextualTransactionVerifier`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation
- Invariant to test: all honest nodes must deterministically accept and reject the same blocks under the same consensus spec
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
