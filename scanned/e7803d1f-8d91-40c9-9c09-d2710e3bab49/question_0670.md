# Q670: High consensus resource amplification in NonContextualTransactionVerifier

## Question
Can an unprivileged attacker repeatedly send small header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a miner on a private chain producing valid-PoW boundary blocks to make `NonContextualTransactionVerifier` in `verification/src/transaction_verifier.rs` amplify CPU, memory, storage, or bandwidth and trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `verification/src/transaction_verifier.rs::NonContextualTransactionVerifier`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
