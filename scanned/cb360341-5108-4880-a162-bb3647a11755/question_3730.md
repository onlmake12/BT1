# Q3730: Critical vm differential path split in new

## Question
Can an unprivileged attacker reach `new` in `script/src/syscalls/close.rs` through two production paths from a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes and make one path accept while the other rejects because of RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/close.rs::new`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
