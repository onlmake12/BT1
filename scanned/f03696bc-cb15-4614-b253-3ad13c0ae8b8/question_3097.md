# Q3097: Critical transaction parser precheck gap in lib

## Question
Can an unprivileged attacker submit malformed-but-reachable cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `lib` in `util/occupied-capacity/core/src/lib.rs` performs expensive or unsafe work before validation and make dependency resolution use a different cell/header than the script-visible authorization path, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/occupied-capacity/core/src/lib.rs::lib`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: make dependency resolution use a different cell/header than the script-visible authorization path
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
