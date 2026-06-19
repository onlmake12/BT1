# Q3738: High vm state transition mismatch in new

## Question
Can an unprivileged attacker enter through a block relayer executing transactions at VM-version or hardfork activation boundaries and sequence cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data so `new` in `script/src/syscalls/current_cycles.rs` observes pre-state and post-state from different views, letting the flow trigger a VM panic or host-side bounds error before the transaction is rejected, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/current_cycles.rs::new`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
