# Q3263: Critical transaction limit off by one in $name_struct

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values through a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values so `$name_struct` in `util/types/src/core/hardfork/helper.rs` bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/hardfork/helper.rs::$name_struct`
- Entrypoint: a script author referencing edge-case cells, headers, DAO data, and occupied-capacity values
- Attacker controls: cell deps, header deps, witnesses, lock/type args, output data lengths, and capacity values
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
