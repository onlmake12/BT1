# Q3580: Critical txpool parser precheck gap in with_tx_pool_read_lock

## Question
Can an unprivileged attacker submit malformed-but-reachable verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a local miner process selecting proposals and uncles near limit boundaries so `with_tx_pool_read_lock` in `tx-pool/src/process.rs` performs expensive or unsafe work before validation and force quadratic graph or selection behavior with few low-cost transactions, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/process.rs::with_tx_pool_read_lock`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
