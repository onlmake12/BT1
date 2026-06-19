# Q3394: Critical txpool cache invalidation failure in default

## Question
Can an unprivileged attacker use a local miner process selecting proposals and uncles near limit boundaries to alternate valid and invalid duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions so `default` in `tx-pool/src/block_assembler/candidate_uncles.rs` leaves a cache, index, or status flag stale and force quadratic graph or selection behavior with few low-cost transactions, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/block_assembler/candidate_uncles.rs::default`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
