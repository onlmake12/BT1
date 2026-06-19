# Q3970: Critical vm batch interaction bug in verify_with_group

## Question
Can an unprivileged attacker batch spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a block relayer executing transactions at VM-version or hardfork activation boundaries so `verify_with_group` in `script/src/type_id.rs` handles the first item safely but applies incorrect assumptions to later items and trigger a VM panic or host-side bounds error before the transaction is rejected, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/type_id.rs::verify_with_group`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
