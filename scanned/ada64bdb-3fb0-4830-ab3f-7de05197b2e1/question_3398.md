# Q3398: Critical txpool parser precheck gap in is_empty

## Question
Can an unprivileged attacker submit malformed-but-reachable duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a local miner process selecting proposals and uncles near limit boundaries so `is_empty` in `tx-pool/src/block_assembler/candidate_uncles.rs` performs expensive or unsafe work before validation and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/block_assembler/candidate_uncles.rs::is_empty`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
