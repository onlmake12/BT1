# Q3455: Critical txpool boundary divergence in get_direct_ids

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions to drive `get_direct_ids` in `tx-pool/src/component/links.rs` across a boundary where pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating the invariant that tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/component/links.rs::get_direct_ids`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
