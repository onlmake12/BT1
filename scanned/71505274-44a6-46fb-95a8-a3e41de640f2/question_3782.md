# Q3782: High vm resource amplification in InheritedFd

## Question
Can an unprivileged attacker repeatedly send small RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a block relayer executing transactions at VM-version or hardfork activation boundaries to make `InheritedFd` in `script/src/syscalls/inherited_fd.rs` amplify CPU, memory, storage, or bandwidth and make VM version gating select the wrong behavior at a hardfork boundary, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/inherited_fd.rs::InheritedFd`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
