# Q3781: High vm canonical encoding ambiguity in InheritedFd

## Question
Can an unprivileged attacker craft alternate encodings for cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data through a block relayer executing transactions at VM-version or hardfork activation boundaries so `InheritedFd` in `script/src/syscalls/inherited_fd.rs` accepts two representations for one security object and make VM version gating select the wrong behavior at a hardfork boundary, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/inherited_fd.rs::InheritedFd`
- Entrypoint: a block relayer executing transactions at VM-version or hardfork activation boundaries
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
