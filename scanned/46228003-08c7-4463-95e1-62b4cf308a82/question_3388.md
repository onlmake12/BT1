# Q3388: Critical txpool replay reorder race in run

## Question
Can an unprivileged attacker replay, reorder, or delay transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `run` in `miner/src/worker/mod.rs` takes a stale branch and force quadratic graph or selection behavior with few low-cost transactions, breaking the invariant that block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/worker/mod.rs::run`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
