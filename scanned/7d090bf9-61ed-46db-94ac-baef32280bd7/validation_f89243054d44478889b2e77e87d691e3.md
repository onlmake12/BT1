The code path is fully traceable. Let me verify the critical links:

**`_process_tx` sets `max_cycles = declared_cycles` directly:** [1](#0-0) 

**`after_process` calls `ban_malformed` when `is_malformed_tx()` is true:** [2](#0-1) 

**`is_malformed_tx()` returns `true` for ALL Script-kind errors except `ARGV_TOO_LONG`:** [3](#0-2) 

**`Reject::Verification` is treated as malformed when the inner error is Script-kind:** [4](#0-3) 

**`add_tx` in the verify queue performs no validation of `declared_cycles`:** [5](#0-4) 

**`submit_remote_tx` passes attacker-controlled `declared_cycles` directly into the pipeline:** [6](#0-5) 

**Worker passes `entry.remote.map(|e| e.0)` (the attacker's declared_cycles) as `declared_cycles` to `_process_tx`:** [7](#0-6) 

---

### Title
Attacker-Controlled `declared_cycles` Causes `ExceededMaximumCycles` to Ban Honest Peers — (`tx-pool/src/process.rs`, `tx-pool/src/verify_mgr.rs`)

### Summary
A remote peer can relay a consensus-valid transaction with `declared_cycles` set to `actual_cycles - 1`. The target node uses this attacker-controlled value as `max_cycles` for script verification, causing `ExceededMaximumCycles` — a Script-kind error — which `is_malformed_tx()` classifies as malformed, triggering a 3-day ban on the honest relaying peer.

### Finding Description

The call chain is:

1. `submit_remote_tx(tx, declared_cycles=actual-1, peer)` — no validation of `declared_cycles`
2. → `resumeble_process_tx` → `enqueue_verify_queue` stores `remote = Some((actual-1, peer))`
3. Worker pops entry, calls `_process_tx(tx, declared_cycles=Some(actual-1), Some(command_rx))`
4. In `_process_tx`:
   ```rust
   let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
   // max_cycles = actual - 1  ← attacker-controlled
   ```
5. `verify_rtx(..., max_cycles=actual-1, ...)` → script needs `actual` cycles → `ExceededMaximumCycles`
6. `try_or_return_with_snapshot!` returns `Err(Reject::Verification(script_error))` **before** the `DeclaredWrongCycles` check is ever reached
7. `after_process` evaluates `reject.is_malformed_tx()`:
   - `Reject::Verification(err)` → `is_malformed_from_verification(err)`
   - `ErrorKind::Script` → `!format!("{}", error).contains(ARGV_TOO_LONG_TEXT)`
   - `ExceededMaximumCycles` message does not contain `ARGV_TOO_LONG_TEXT` → returns `true`
8. `ban_malformed(peer, ...)` bans the honest peer for 3 days

The `DeclaredWrongCycles` guard (lines 736–749 of `process.rs`) is never reached because `try_or_return_with_snapshot!` exits early on the verification error. There is no lower-bound check on `declared_cycles` anywhere in `add_tx`, `resumeble_process_tx`, or `submit_remote_tx`.

### Impact Explanation
An attacker who controls a single node can selectively ban any honest peer from any target node by relaying a valid transaction with `declared_cycles = actual - 1`. The ban lasts 3 days. By repeating this with different valid transactions against different peers, the attacker can exhaust a node's peer slots, causing network partition. Nodes that lose peers cannot receive blocks or transactions needed for consensus participation.

### Likelihood Explanation
The `declared_cycles` field in the `RelayTransactions` P2P message is fully attacker-controlled with no authentication. The attacker only needs to know the actual cycle count of any valid transaction (trivially obtained by running the script locally or observing a successful relay). The attack requires no hashpower, no keys, and no privileged access — only a P2P connection to the target node.

### Recommendation
In `_process_tx`, do not use `declared_cycles` directly as `max_cycles`. Instead, always verify with `self.consensus.max_block_cycles()` (or `max_tx_verify_cycles`) as the upper bound, and only use `declared_cycles` for the post-verification equality check. The fix:

```rust
// Use consensus max, not declared_cycles, as the verification limit
let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
// ... run verify_rtx with max_cycles ...
// Then check declared matches actual:
if let Some(declared) = declared_cycles && declared != verified.cycles {
    return Some((Err(Reject::DeclaredWrongCycles(declared, verified.cycles)), snapshot));
}
```

`DeclaredWrongCycles` is already correctly classified as `is_malformed_tx() = true` but also `is_allowed_relay() = true`, which is the appropriate response — reject the tx but do not ban the peer, since the peer may have received the tx with wrong cycles from a third party.

### Proof of Concept
1. Craft a transaction using `always_success` lock (537 cycles actual).
2. Connect to target node as peer A.
3. Send `RelayTransactions` with `declared_cycles = 536`.
4. Observe: target node runs `verify_rtx` with `max_cycles = 536`, gets `ExceededMaximumCycles`, calls `ban_malformed(peer_A)`.
5. Connect as peer B, relay same tx with `declared_cycles = 537`.
6. Observe: tx accepted into pool; peer A remains banned for 3 days.

### Citations

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }
```

**File:** tx-pool/src/process.rs (L514-515)
```rust
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
```

**File:** tx-pool/src/process.rs (L720-732)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
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

**File:** util/types/src/core/tx_pool.rs (L89-97)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            Reject::Malformed(_, _) => true,
            Reject::DeclaredWrongCycles(..) => true,
            Reject::Verification(err) => is_malformed_from_verification(err),
            Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
            _ => false,
        }
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L198-236)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
```

**File:** tx-pool/src/verify_mgr.rs (L147-162)
```rust
            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
            } else {
                info!("_process_tx for tx: {} returned none", entry.tx.hash());
            }
        }
```
