# Q3677: High txpool differential path split in gap

## Question
Can an unprivileged attacker reach `gap` in `util/proposal-table/src/lib.rs` through two production paths from a local miner process selecting proposals and uncles near limit boundaries and make one path accept while the other rejects because of duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/proposal-table/src/lib.rs::gap`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
