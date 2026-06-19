# Q3247: Critical transaction replay reorder race in new_mirana

## Question
Can an unprivileged attacker replay, reorder, or delay cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `new_mirana` in `util/types/src/core/hardfork/ckb2021.rs` takes a stale branch and make dependency resolution use a different cell/header than the script-visible authorization path, breaking the invariant that resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/types/src/core/hardfork/ckb2021.rs::new_mirana`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
