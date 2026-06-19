# Q3716: Critical vm differential path split in peek

## Question
Can an unprivileged attacker reach `peek` in `script/src/scheduler.rs` through two production paths from a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes and make one path accept while the other rejects because of RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/scheduler.rs::peek`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
