# Q3295: Critical transaction limit off by one in BlockIssuance

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a tx-pool submitter racing mempool admission against chain reorg or cell status changes so `BlockIssuance` in `util/types/src/core/reward.rs` make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/reward.rs::BlockIssuance`
- Entrypoint: a tx-pool submitter racing mempool admission against chain reorg or cell status changes
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
