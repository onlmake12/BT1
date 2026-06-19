# Q3376: Critical txpool canonical encoding ambiguity in poll_worker_message

## Question
Can an unprivileged attacker craft alternate encodings for block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `poll_worker_message` in `miner/src/worker/eaglesong_simple.rs` accepts two representations for one security object and pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `miner/src/worker/eaglesong_simple.rs::poll_worker_message`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: pollute orphan/recent-reject/verification queues so one attacker consumes disproportionate resources
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
