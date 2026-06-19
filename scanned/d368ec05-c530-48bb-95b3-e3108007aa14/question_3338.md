# Q3338: High txpool boundary divergence in poll_block_template

## Question
Can an unprivileged attacker enter through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions and use verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing to drive `poll_block_template` in `miner/src/client.rs` across a boundary where pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating the invariant that block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `miner/src/client.rs::poll_block_template`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
