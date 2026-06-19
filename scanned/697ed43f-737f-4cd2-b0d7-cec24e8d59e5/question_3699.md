# Q3699: High vm boundary divergence in test_vm_internal_error_preserves_text

## Question
Can an unprivileged attacker enter through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and use malformed buffers, return codes, memory pointers, and script group composition to drive `test_vm_internal_error_preserves_text` in `script/src/error.rs` across a boundary where make VM version gating select the wrong behavior at a hardfork boundary, violating the invariant that CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/error.rs::test_vm_internal_error_preserves_text`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
