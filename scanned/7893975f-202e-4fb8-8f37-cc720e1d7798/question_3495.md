# Q3495: Critical txpool limit off by one in get_estimate_total_keys_num

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a local miner process selecting proposals and uncles near limit boundaries so `get_estimate_total_keys_num` in `tx-pool/src/component/recent_reject.rs` pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/recent_reject.rs::get_estimate_total_keys_num`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
