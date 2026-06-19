# Q3663: Critical txpool boundary divergence in lib

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions to drive `lib` in `util/fee-estimator/src/lib.rs` across a boundary where pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating the invariant that valid user transactions must not be persistently censored by cheap attacker-created pool state, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/lib.rs::lib`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
