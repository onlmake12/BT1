# Q3452: Critical txpool boundary divergence in calc_descendants

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status to drive `calc_descendants` in `tx-pool/src/component/links.rs` across a boundary where make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating the invariant that tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/component/links.rs::calc_descendants`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
