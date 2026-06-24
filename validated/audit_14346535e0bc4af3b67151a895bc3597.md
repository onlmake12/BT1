Audit Report

## Title
Incorrect Peer Banning via `ScriptError::InvalidVmVersion` Classified as Malformed — (`util/types/src/core/tx_pool.rs`, `tx-pool/src/process.rs`)

## Summary

A peer relaying a transaction with a `Data2` (`hash_type = 0x04`) type script on an output to a node where RFC-0049 is not yet active will be incorrectly banned for 3 days. The transaction passes `ScriptHashTypeVerifier` (which only checks output lock scripts against `ENABLED_SCRIPT_HASH_TYPE`, not output type scripts), then fails contextual verification with `ScriptError::InvalidVmVersion(2)` when the output type script is executed. This error is unconditionally classified as malformed by `is_malformed_from_verification`, triggering `ban_malformed` in `after_process`. The invariant that peer banning is reserved for structurally malformed transactions is violated.

## Finding Description

**Step 1 — Non-contextual check passes for `Data2`.**

`ENABLED_SCRIPT_HASH_TYPE` explicitly includes `4u8` (`Data2`): [1](#0-0) 

`ScriptHashTypeVerifier::verify()` checks only output lock scripts against this set, with no reference to RFC-0049 activation epoch, and does not check output type scripts at all: [2](#0-1) 

A transaction with a `Data2` output type script passes this check entirely, since only `output.lock().hash_type()` is inspected.

**Step 2 — Contextual verification fails with `InvalidVmVersion(2)` before RFC-0049.**

During contextual verification, output type scripts are executed. `select_version()` checks `is_vm_version_2_and_syscalls_3_enabled()` (epoch-gated via RFC-0049). Before activation, it returns `Err(ScriptError::InvalidVmVersion(2))`: [3](#0-2) 

`is_vm_version_2_and_syscalls_3_enabled` reads the hardfork switch against `epoch_number_without_proposal_window()`: [4](#0-3) 

**Step 3 — `InvalidVmVersion(2)` is classified as malformed.**

`ScriptError::InvalidVmVersion` is converted to `ErrorKind::Script` (not `ErrorKind::Internal`) via the `From<TransactionScriptError> for Error` impl — the `_` arm catches it: [5](#0-4) 

`is_malformed_from_verification` treats **all** `ErrorKind::Script` errors as malformed, with the sole exception of errors whose formatted string contains `ARGV_TOO_LONG_TEXT`: [6](#0-5) 

`ScriptError::InvalidVmVersion(2)` formats as `"Invalid VM Version: 2"`, which does not contain `ARGV_TOO_LONG_TEXT`, so `is_malformed_tx()` returns `true`: [7](#0-6) 

**Step 4 — Peer is banned in `after_process`.**

When contextual verification returns `Reject::Verification(InvalidVmVersion(2))` for a remote-sourced transaction, `after_process` calls `ban_malformed`, issuing a 3-day ban: [8](#0-7) [9](#0-8) 

**Note on the submitted PoC:** The PoC states "output lock script has `hash_type = Data2`." Output lock scripts are not executed during the transaction's own contextual verification — they are only executed when those outputs are later spent. The correct attack vector is an **output type script** with `hash_type = Data2`, which IS executed during contextual verification of the submitting transaction. The core vulnerability is real; only the specific PoC step 2 description is imprecise.

## Impact Explanation

This maps to **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker can repeatedly connect to CKB nodes as a P2P peer and relay transactions containing `Data2` output type scripts. Each such transaction causes the victim node to ban the relaying peer for 72 hours. By targeting multiple nodes simultaneously, an attacker can cause widespread peer banning during the pre-RFC-0049 epoch window, degrading network connectivity and causing effective network fragmentation without requiring any privileged access.

## Likelihood Explanation

This is exploitable by any unprivileged P2P peer during the pre-RFC-0049 epoch window. No special privileges, leaked keys, or victim mistakes are required. The attacker only needs to establish a P2P connection and relay a crafted transaction. The attack is repeatable: after being banned, the attacker can reconnect from a different IP and repeat. The cost is negligible (crafting a transaction with a `Data2` type script requires no funds, as the transaction need not be valid beyond passing non-contextual checks).

## Recommendation

`is_malformed_from_verification` must not treat `ScriptError::InvalidVmVersion` as malformed. This error is epoch-dependent (hardfork timing), not structural. Two options:

1. Add an exclusion for `InvalidVmVersion` in `is_malformed_from_verification` analogous to the existing `ARGV_TOO_LONG_TEXT` exclusion — check if the formatted error string contains `"Invalid VM Version"`.
2. Introduce a finer-grained classification in `ScriptError` or `TransactionScriptError` that distinguishes timing-dependent invalidity (hardfork-gated) from structural malformation, and route `InvalidVmVersion` to a non-banning rejection path.

## Proof of Concept

1. Configure a CKB node with RFC-0049 activation epoch set to a future epoch (e.g., epoch 1000).
2. As a P2P peer, send a `RelayTransaction` containing a transaction with an output cell whose **type script** has `hash_type = 0x04` (Data2).
3. The node's `ScriptHashTypeVerifier` passes (it only checks output lock scripts; type scripts are not checked).
4. Contextual verification executes the output type script → `select_version()` → `Err(ScriptError::InvalidVmVersion(2))`.
5. `From<TransactionScriptError> for Error` maps this to `ErrorKind::Script`.
6. `is_malformed_from_verification` returns `true` (Script kind, no ARGV_TOO_LONG match).
7. `after_process` calls `ban_malformed(peer, ...)` → `network.ban_peer(peer, 3 days, ...)`.
8. Confirm via `get_banned_addresses` RPC that the peer is banned for 72 hours.

### Citations

**File:** util/constant/src/consensus.rs (L7-11)
```rust
pub static ENABLED_SCRIPT_HASH_TYPE: Set<u8> = phf_set! {
    0u8, // ScriptHashType::Data
    1u8, // ScriptHashType::Type
    2u8, // ScriptHashType::Data1
    4u8, // ScriptHashType::Data2
```

**File:** verification/src/transaction_verifier.rs (L796-814)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        for output in self.transaction.outputs() {
            if let Ok(hash_type) = TryInto::<ScriptHashType>::try_into(output.lock().hash_type()) {
                let val: u8 = hash_type.into();
                if !ENABLED_SCRIPT_HASH_TYPE.contains(&val) {
                    return Err(
                        TransactionError::ScriptHashTypeNotPermitted { hash_type: val }.into(),
                    );
                }
            } else {
                return Err((TransactionError::InvalidScriptHashType {
                    hash_type: output.lock().hash_type(),
                })
                .into());
            }
        }

        Ok(())
    }
```

**File:** script/src/types.rs (L887-897)
```rust
    fn is_vm_version_2_and_syscalls_3_enabled(&self) -> bool {
        // If the proposal window is allowed to prejudge on the vm version,
        // it will cause proposal tx to start a new vm in the blocks before hardfork,
        // destroying the assumption that the transaction execution only uses the old vm
        // before hardfork, leading to unexpected network splits.
        let epoch_number = self.tx_env.epoch_number_without_proposal_window();
        let hardfork_switch = self.consensus.hardfork_switch();
        hardfork_switch
            .ckb2023
            .is_vm_version_2_and_syscalls_3_enabled(epoch_number)
    }
```

**File:** script/src/types.rs (L914-919)
```rust
            ScriptHashType::Data2 => {
                if is_vm_version_2_and_syscalls_3_enabled {
                    Ok(ScriptVersion::V2)
                } else {
                    Err(ScriptError::InvalidVmVersion(2))
                }
```

**File:** script/src/error.rs (L41-43)
```rust
    /// InvalidVmVersion
    #[error("Invalid VM Version: {0}")]
    InvalidVmVersion(u8),
```

**File:** script/src/error.rs (L195-203)
```rust
impl From<TransactionScriptError> for Error {
    fn from(error: TransactionScriptError) -> Self {
        match error.cause {
            ScriptError::Interrupts => ErrorKind::Internal
                .because(InternalErrorKind::Interrupts.other(ScriptError::Interrupts.to_string())),
            _ => ErrorKind::Script.because(error),
        }
    }
}
```

**File:** util/types/src/core/tx_pool.rs (L69-85)
```rust
fn is_malformed_from_verification(error: &Error) -> bool {
    match error.kind() {
        ErrorKind::Transaction => error
            .downcast_ref::<TransactionError>()
            .expect("error kind checked")
            .is_malformed_tx(),
        ErrorKind::Script => !format!("{}", error).contains(ARGV_TOO_LONG_TEXT),
        ErrorKind::Internal => {
            error
                .downcast_ref::<InternalError>()
                .expect("error kind checked")
                .kind()
                == InternalErrorKind::CapacityOverflow
        }
        _ => false,
    }
}
```

**File:** tx-pool/src/process.rs (L513-516)
```rust
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
```

**File:** tx-pool/src/process.rs (L679-702)
```rust
    async fn ban_malformed(&self, peer: PeerIndex, reason: String) {
        const DEFAULT_BAN_TIME: Duration = Duration::from_secs(3600 * 24 * 3);

        #[cfg(feature = "with_sentry")]
        use sentry::{Level, capture_message, with_scope};

        #[cfg(feature = "with_sentry")]
        with_scope(
            |scope| scope.set_fingerprint(Some(&["ckb-tx-pool", "receive-invalid-remote-tx"])),
            || {
                capture_message(
                    &format!(
                        "Ban peer {} for {} seconds, reason: \
                        {}",
                        peer,
                        DEFAULT_BAN_TIME.as_secs(),
                        reason
                    ),
                    Level::Info,
                )
            },
        );
        self.network.ban_peer(peer, DEFAULT_BAN_TIME, reason);
        self.verify_queue.write().await.remove_txs_by_peer(&peer);
```
