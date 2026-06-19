# Q3790: Critical vm differential path split in new

## Question
Can an unprivileged attacker reach `new` in `script/src/syscalls/inherited_fd.rs` through two production paths from a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and make one path accept while the other rejects because of cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/inherited_fd.rs::new`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
