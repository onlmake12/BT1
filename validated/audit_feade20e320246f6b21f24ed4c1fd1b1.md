Audit Report

## Title
Attacker-Controlled `declared_cycles` Used as `max_cycles` Causes Honest Peer Ban — (`tx-pool/src/process.rs`, `tx-pool/src/verify_mgr.rs`)

## Summary
In `_process_tx`, the `declared_cycles` value supplied by a remote peer is used directly as `max_cycles` for script verification. An attacker can relay a valid transaction with `declared_cycles = actual_cycles - 1`, causing `ExceededMaximumCycles` — a Script-kind error — which `is_malformed_tx()` classifies as malformed, triggering a 3-day ban on the honest relaying peer. The `DeclaredWrongCycles` guard that would correctly handle cycle mismatches is never reached because `try_or_return_with_snapshot!` exits early on the verification failure.

## Finding Description

**Step 1:** `submit_remote_tx` accepts `declared_cycles` from the P2P message with no validation and passes it directly into the pipeline. [1](#0-0) 

**Step 2:** The worker extracts `entry.remote.map(|e| e.0)` (the attacker's `declared_cycles`) and passes it to `_process_tx`. [2](#0-1) 

**Step 3:** Inside `_process_tx`, `declared_cycles` is used directly as `max_cycles`:
```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [3](#0-2) 

**Step 4:** If `declared_cycles = actual - 1`, `verify_rtx` returns `ScriptError::ExceededMaximumCycles`, which is converted to `ErrorKind::Script` (all `ScriptError` variants except `Interrupts` map to `ErrorKind::Script`). [4](#0-3) 

**Step 5:** `try_or_return_with_snapshot!(verified_ret, snapshot)` at line 734 exits early on this error. The `DeclaredWrongCycles` guard at lines 736–749 is **never reached**. [5](#0-4) 

**Step 6:** `after_process` calls `reject.is_malformed_tx()`, which calls `is_malformed_from_verification`. For `ErrorKind::Script`, this returns `true` unless the error string contains `ARGV_TOO_LONG_TEXT`. The `ExceededMaximumCycles` message (`"ExceededMaximumCycles: expect cycles <= {0}"`) does not contain that text, so it returns `true`. [6](#0-5) 

**Step 7:** `ban_malformed(peer, ...)` is called, banning the honest relaying peer for 3 days. [7](#0-6) 

## Impact Explanation
An attacker with a single P2P connection can selectively ban any honest peer from a target node for 3 days per attack. By repeating this with different valid transactions against different peers, the attacker can exhaust the target node's peer slots, causing network isolation. An isolated node cannot receive blocks or transactions, preventing consensus participation. This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation
The `declared_cycles` field in the `RelayTransactions` P2P message is fully attacker-controlled with no authentication or bounds checking anywhere in `add_tx`, `resumeble_process_tx`, or `submit_remote_tx`. The attacker only needs to know the actual cycle count of any valid transaction, which is trivially obtained by running the script locally. No hashpower, keys, or privileged access are required — only a standard P2P connection to the target node.

## Recommendation
In `_process_tx`, do not use `declared_cycles` as `max_cycles`. Always verify with the consensus maximum as the upper bound, then perform the equality check afterward:

```rust
// Use consensus max (or max_tx_verify_cycles) as the verification limit
let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
let verified = try_or_return_with_snapshot!(
    verify_rtx(Arc::clone(&snapshot), Arc::clone(&rtx), tx_env, &verify_cache, max_cycles, command_rx).await,
    snapshot
);
// Now check declared matches actual
if let Some(declared) = declared_cycles {
    if declared != verified.cycles {
        return Some((Err(Reject::DeclaredWrongCycles(declared, verified.cycles)), snapshot));
    }
}
```

`DeclaredWrongCycles` is already handled correctly: `is_malformed_tx() = true` but `is_allowed_relay() = true`, so the tx is rejected without banning the peer. [8](#0-7) 

## Proof of Concept
1. Obtain a valid transaction using `always_success` lock (537 cycles actual).
2. Connect to the target node as peer A.
3. Send a `RelayTransactions` P2P message with `declared_cycles = 536`.
4. Observe: target node runs `verify_rtx` with `max_cycles = 536`, receives `ExceededMaximumCycles`, calls `ban_malformed(peer_A)` — peer A is banned for 3 days.
5. Connect as peer B, relay the same transaction with `declared_cycles = 537`.
6. Observe: transaction is accepted into the pool; peer A remains banned.
7. Repeat step 2–4 with different peers to exhaust the target node's peer slots.

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

**File:** tx-pool/src/process.rs (L734-749)
```rust
        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }
```

**File:** tx-pool/src/verify_mgr.rs (L147-158)
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

**File:** util/types/src/core/tx_pool.rs (L69-97)
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

impl Reject {
    /// Returns true if the reject reason is malformed tx.
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

**File:** util/types/src/core/tx_pool.rs (L110-113)
```rust
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```
