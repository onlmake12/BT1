# Q3602: Critical txpool canonical encoding ambiguity in VerifyMgr

## Question
Can an unprivileged attacker craft alternate encodings for block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a local miner process selecting proposals and uncles near limit boundaries so `VerifyMgr` in `tx-pool/src/verify_mgr.rs` accepts two representations for one security object and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/verify_mgr.rs::VerifyMgr`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
