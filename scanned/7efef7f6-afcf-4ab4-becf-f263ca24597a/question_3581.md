# Q3581: Critical txpool boundary divergence in call

## Question
Can an unprivileged attacker enter through a peer relaying transactions that race recent-reject, orphan, and verification-queue state and use duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions to drive `call` in `tx-pool/src/service.rs` across a boundary where make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating the invariant that block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/service.rs::call`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
