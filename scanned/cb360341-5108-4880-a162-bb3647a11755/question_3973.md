# Q3973: High vm batch interaction bug in from_cell_meta

## Question
Can an unprivileged attacker batch spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a block relayer executing transactions at VM-version or hardfork activation boundaries so `from_cell_meta` in `script/src/types.rs` handles the first item safely but applies incorrect assumptions to later items and trigger a VM panic or host-side bounds error before the transaction is rejected, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/types.rs::from_cell_meta`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
