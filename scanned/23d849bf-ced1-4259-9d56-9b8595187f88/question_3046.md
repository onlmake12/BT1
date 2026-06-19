# Q3046: High transaction limit off by one in From

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a block relayer including dependency-heavy transactions in an otherwise valid block so `From` in `util/dao/utils/src/error.rs` make dependency resolution use a different cell/header than the script-visible authorization path, violating tx-pool admission and block verification must not diverge for security-relevant validity, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/dao/utils/src/error.rs::From`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: tx-pool admission and block verification must not diverge for security-relevant validity
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
