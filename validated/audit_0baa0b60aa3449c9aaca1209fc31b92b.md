All cited code references have been verified against the actual repository. Every step in the execution path is confirmed:

- `ENABLED_SCRIPT_HASH_TYPE` includes `4u8` with no epoch guard [1](#0-0) 
- `ScriptHashTypeVerifier::verify()` checks only against that set, no hardfork context [2](#0-1) 
- `select_version()` returns `Err(ScriptError::InvalidVmVersion(2))` for `Data2` before RFC-0049 activation [3](#0-2) 
- `is_vm_version_2_and_syscalls_3_enabled` is epoch-gated [4](#0-3) 
- `From<TransactionScriptError> for Error` routes `InvalidVmVersion` to `ErrorKind::Script` [5](#0-4) 
- `is_malformed_from_verification` treats all `ErrorKind::Script` as malformed except `ARGV_TOO_LONG_TEXT` [6](#0-5) 
- `"Invalid VM Version: 2"` does not contain `ARGV_TOO_LONG_TEXT` [7](#0-6) 
- `after_process` calls `ban_malformed` when `is_malformed_tx()` is true [8](#0-7) 
- `ban_malformed` issues a 72-hour ban (`3600 * 24 * 3`) [9](#0-8) 

---

Audit Report

## Title
`ScriptError::InvalidVmVersion` Unconditionally Classified as Malformed, Causing Incorrect 72-Hour Peer Bans — (`util/types/src/core/tx_pool.rs`, `tx-pool/src/process.rs`)

## Summary

Before RFC-0049 (`ckb2023`) activation, a peer relaying a `Data2` (`hash_type = 0x04`) transaction passes `NonContextualTransactionVerifier` (because `Data2` is in `ENABLED_SCRIPT_HASH_TYPE`) but fails contextual verification with `ScriptError::InvalidVmVersion(2)`. This error is unconditionally classified as malformed by `is_malformed_from_verification`, causing `after_process` to invoke `ban_malformed` and issue a 72-hour ban against the relaying peer. Epoch-gated invalidity is treated identically to structural corruption.

## Finding Description

**Step 1 — Non-contextual check passes for `Data2`.**

`ENABLED_SCRIPT_HASH_TYPE` explicitly includes `4u8` (Data2). `ScriptHashTypeVerifier::verify()` checks only against this set with no reference to RFC-0049 activation epoch, so a `Data2` transaction passes non-contextual verification unconditionally.

**Step 2 — Contextual verification fails with `InvalidVmVersion(2)` before RFC-0049.**

`select_version()` in `script/src/types.rs` checks `is_vm_version_2_and_syscalls_3_enabled()`, which reads the epoch-gated RFC-0049 flag via `epoch_number_without_proposal_window()`. Before activation, the `Data2` branch returns `Err(ScriptError::InvalidVmVersion(2))`.

**Step 3 — `InvalidVmVersion(2)` is classified as malformed.**

`From<TransactionScriptError> for Error` routes all `ScriptError` variants except `Interrupts` to `ErrorKind::Script`. `is_malformed_from_verification` treats every `ErrorKind::Script` error as malformed unless the formatted string contains `ARGV_TOO_LONG_TEXT`. `ScriptError::InvalidVmVersion(2)` formats as `"Invalid VM Version: 2"`, which does not contain that substring, so `is_malformed_tx()` returns `true`.

**Step 4 — Peer is banned.**

`after_process` calls `ban_malformed(peer, ...)` when `reject.is_malformed_tx()` is true for a remote-sourced transaction. `ban_malformed` issues a 72-hour ban (`Duration::from_secs(3600 * 24 * 3)`).

**Why existing guards fail:** The sole carve-out in `is_malformed_from_verification` for `ErrorKind::Script` is the `ARGV_TOO_LONG_TEXT` string check. There is no carve-out for epoch-dependent errors such as `InvalidVmVersion`. The non-contextual verifier's inclusion of `Data2` in `ENABLED_SCRIPT_HASH_TYPE` is intentional (to allow the type post-activation), but it creates a gap: the non-contextual pass combined with the contextual fail produces a `Script`-kind error that the banning logic cannot distinguish from a genuinely malformed transaction.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can, at negligible cost (one crafted `Data2` transaction per target peer), cause any CKB node operating before RFC-0049 activation to ban legitimate relay peers for 72 hours each. Systematically applied across the relay graph during the pre-hardfork window, this degrades network connectivity and can isolate nodes, disrupting transaction propagation across the network.

## Likelihood Explanation

Exploitable by any unprivileged P2P peer during the pre-RFC-0049 epoch window. No special privileges, keys, or victim mistakes are required. The attacker only needs to connect to a target node and relay a single transaction with `hash_type = 0x04`. The attack is repeatable against any number of nodes and can be automated. The pre-hardfork window is a known, predictable time period, making targeted exploitation straightforward.

## Recommendation

`is_malformed_from_verification` must not treat `ScriptError::InvalidVmVersion` as malformed. Two concrete options:

1. Add an `InvalidVmVersion` exclusion in `is_malformed_from_verification` analogous to the existing `ARGV_TOO_LONG_TEXT` exclusion — downcast the root cause to `TransactionScriptError`, inspect `script_error()`, and return `false` if it is `ScriptError::InvalidVmVersion(_)`.
2. Introduce a finer-grained classification on `ScriptError` (e.g., an `is_epoch_dependent()` predicate) and consult it in `is_malformed_from_verification` before returning `true` for `ErrorKind::Script`.

Either fix ensures that timing-dependent invalidity does not trigger peer banning.

## Proof of Concept

1. Configure a CKB node with RFC-0049 (`ckb2023`) activation epoch set to a future epoch (e.g., epoch 10000).
2. As a P2P peer, connect to the node and send a `RelayTransaction` message containing a transaction whose output lock script has `hash_type = 0x04` (Data2).
3. Observe that `NonContextualTransactionVerifier` passes (Data2 is in `ENABLED_SCRIPT_HASH_TYPE`).
4. Observe that contextual verification calls `select_version()` → `Err(ScriptError::InvalidVmVersion(2))`.
5. Observe that `after_process` calls `is_malformed_tx()` → `true` → `ban_malformed(peer, ...)`.
6. Confirm the peer is banned for 72 hours via the `get_banned_addresses` RPC.

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

**File:** script/src/error.rs (L195-202)
```rust
impl From<TransactionScriptError> for Error {
    fn from(error: TransactionScriptError) -> Self {
        match error.cause {
            ScriptError::Interrupts => ErrorKind::Internal
                .because(InternalErrorKind::Interrupts.other(ScriptError::Interrupts.to_string())),
            _ => ErrorKind::Script.because(error),
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
