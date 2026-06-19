# Q3739: High vm resource amplification in new

## Question
Can an unprivileged attacker repeatedly send small RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a transaction sender deploying a crafted CKB-VM script and witness payload to make `new` in `script/src/syscalls/current_cycles.rs` amplify CPU, memory, storage, or bandwidth and undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/current_cycles.rs::new`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
