# Q3532: High txpool canonical encoding ambiguity in BlockAssemblerError

## Question
Can an unprivileged attacker craft alternate encodings for verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `BlockAssemblerError` in `tx-pool/src/error.rs` accepts two representations for one security object and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/error.rs::BlockAssemblerError`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
