# Q3729: Critical vm canonical encoding ambiguity in new

## Question
Can an unprivileged attacker craft alternate encodings for cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data through a block relayer executing transactions at VM-version or hardfork activation boundaries so `new` in `script/src/syscalls/close.rs` accepts two representations for one security object and trigger a VM panic or host-side bounds error before the transaction is rejected, violating CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/close.rs::new`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
