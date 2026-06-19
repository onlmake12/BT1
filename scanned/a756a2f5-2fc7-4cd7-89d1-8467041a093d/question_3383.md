# Q3383: Critical txpool parser precheck gap in WorkerMessage

## Question
Can an unprivileged attacker submit malformed-but-reachable verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `WorkerMessage` in `miner/src/worker/mod.rs` performs expensive or unsafe work before validation and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/worker/mod.rs::WorkerMessage`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
