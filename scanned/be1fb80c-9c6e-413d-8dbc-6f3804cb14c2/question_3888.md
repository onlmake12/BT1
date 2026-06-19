# Q3888: High vm differential path split in new

## Question
Can an unprivileged attacker reach `new` in `script/src/syscalls/pipe.rs` through two production paths from a block relayer executing transactions at VM-version or hardfork activation boundaries and make one path accept while the other rejects because of malformed buffers, return codes, memory pointers, and script group composition, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/pipe.rs::new`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
