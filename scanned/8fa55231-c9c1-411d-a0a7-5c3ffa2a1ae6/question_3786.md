# Q3786: Critical vm batch interaction bug in initialize

## Question
Can an unprivileged attacker batch RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a block relayer executing transactions at VM-version or hardfork activation boundaries so `initialize` in `script/src/syscalls/inherited_fd.rs` handles the first item safely but applies incorrect assumptions to later items and trigger a VM panic or host-side bounds error before the transaction is rejected, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/inherited_fd.rs::initialize`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
