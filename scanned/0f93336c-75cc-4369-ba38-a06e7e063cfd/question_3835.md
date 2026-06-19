# Q3835: High vm limit off by one in ecall

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data through a block relayer executing transactions at VM-version or hardfork activation boundaries so `ecall` in `script/src/syscalls/load_script_hash.rs` make a syscall expose bytes that differ from consensus-resolved transaction data, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/load_script_hash.rs::ecall`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
