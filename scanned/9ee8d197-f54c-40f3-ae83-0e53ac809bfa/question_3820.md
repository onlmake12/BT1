# Q3820: High vm parser precheck gap in new

## Question
Can an unprivileged attacker submit malformed-but-reachable RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a block relayer executing transactions at VM-version or hardfork activation boundaries so `new` in `script/src/syscalls/load_input.rs` performs expensive or unsafe work before validation and undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/load_input.rs::new`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
