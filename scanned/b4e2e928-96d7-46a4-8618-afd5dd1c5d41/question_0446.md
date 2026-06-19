# Q446: High consensus boundary divergence in need_clean

## Question
Can an unprivileged attacker enter through a miner on a private chain producing valid-PoW boundary blocks and use header timestamp, compact target, epoch fraction, nonce, parent hash, and block number to drive `need_clean` in `chain/src/utils/orphan_block_pool.rs` across a boundary where make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation, violating the invariant that invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `chain/src/utils/orphan_block_pool.rs::need_clean`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
