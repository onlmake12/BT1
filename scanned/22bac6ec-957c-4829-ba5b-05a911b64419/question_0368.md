# Q368: Critical consensus replay reorder race in non_contextual_verify

## Question
Can an unprivileged attacker replay, reorder, or delay header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a miner on a private chain producing valid-PoW boundary blocks so `non_contextual_verify` in `chain/src/chain_service.rs` takes a stale branch and make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation, breaking the invariant that malformed consensus objects must return structured errors without node panic or unbounded work, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `chain/src/chain_service.rs::non_contextual_verify`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation
- Invariant to test: malformed consensus objects must return structured errors without node panic or unbounded work
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
