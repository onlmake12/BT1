# Q3859: High vm limit off by one in witnesses

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles so `witnesses` in `script/src/syscalls/load_witness.rs` make VM version gating select the wrong behavior at a hardfork boundary, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/load_witness.rs::witnesses`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
