I have traced the full call chain through the codebase. Here is my analysis.

---

## Full Code Path Trace

**Step 1 — Non-contextual verification passes**

`NonContextualTransactionVerifier::verify()` checks only: version, size, empty, duplicate deps, outputs-data length, and script hash type. [1](#0-0) 

There is **no capacity-sum check** here. A transaction with two outputs each carrying `u64::MAX/2 + 1` shannons passes cleanly.

**Step 2 — Resolve succeeds without ownership**

`pre_check()` calls `resolve_tx()`. This only checks whether the referenced UTXOs **exist** in the UTXO set — it does not verify the attacker controls them. Script verification (ownership) happens later. [2](#0-1) 

The attacker can reference **any** live UTXO found by scanning the public chain.

**Step 3 — `check_tx_fee` overflows first**

After resolve, `pre_check()` calls `check_tx_fee()`, which calls `DaoCalculator::transaction_fee()`:

```rust
rtx.transaction
    .outputs_capacity()          // ← safe_add overflows here
    .and_then(|y| maximum_withdraw.safe_sub(y))
    .map_err(Into::into)
``` [3](#0-2) 

`outputs_capacity()` calls `safe_add` on the two `u64::MAX/2 + 1` values: [4](#0-3) 

The overflow is caught by `checked_add`, returning `ckb_occupied_capacity::Error::Overflow`. `check_tx_fee` maps this to:

```rust
Reject::Malformed(format!("{err}"), "expect (outputs capacity) <= (inputs capacity)")
``` [5](#0-4) 

`Reject::Malformed` is **unconditionally malformed**: [6](#0-5) 

**Step 4 — Alternate path: `CapacityVerifier::verify()` also overflows**

Even if `check_tx_fee` were skipped, `CapacityVerifier::verify()` calls `outputs_capacity()` at line 485, which overflows identically. Via `error/src/convert.rs`: [7](#0-6) 

…the error becomes `InternalError{kind: CapacityOverflow}`, wrapped as `Reject::Verification(Error{kind: Internal, …})`. Then `is_malformed_from_verification()` explicitly returns `true` for this kind: [8](#0-7) 

**Step 5 — Peer is banned**

`after_process()` sees `reject.is_malformed_tx() == true` and calls `ban_malformed(peer, …)`: [9](#0-8) [10](#0-9) 

Ban duration is **3 days** (`3600 * 24 * 3` seconds).

---

## Verdict

### Title
Arithmetic overflow in `outputs_capacity()` triggers peer ban via `Reject::Malformed` / `InternalErrorKind::CapacityOverflow` without requiring UTXO ownership — (`tx-pool/src/util.rs`, `util/types/src/core/tx_pool.rs`, `error/src/convert.rs`)

### Summary
An unprivileged remote peer can relay a structurally valid transaction whose output capacities overflow `u64` when summed. This causes `check_tx_fee` (or `CapacityVerifier::verify`) to return a "malformed" rejection, triggering a 3-day peer ban. No CKB ownership is required — only a reference to any live UTXO.

### Finding Description
`NonContextualTransactionVerifier` performs no capacity-sum check. After resolve succeeds (inputs exist, no ownership check), `check_tx_fee` calls `DaoCalculator::transaction_fee()` → `outputs_capacity()` → `Capacity::safe_add` → `checked_add` returns `None` → `ckb_occupied_capacity::Error::Overflow`. This is mapped to `Reject::Malformed(...)`, which `is_malformed_tx()` treats as a peer-ban trigger. Separately, `is_malformed_from_verification()` also explicitly maps `InternalErrorKind::CapacityOverflow` to `true`, so the `CapacityVerifier` path is equally affected.

### Impact Explanation
Any peer that relays such a transaction is banned for 72 hours. An attacker with rotating IPs and knowledge of any live UTXO (public chain data) can repeatedly trigger bans across the network at near-zero cost, degrading peer connectivity and causing network congestion.

### Likelihood Explanation
The preconditions are trivially met: (1) find any live UTXO by scanning the chain, (2) craft a ~200-byte transaction, (3) connect via P2P. No CKB tokens, no keys, no privileged access required.

### Recommendation
- In `check_tx_fee`, map capacity overflow to `Reject::LowFeeRate` or a non-malformed rejection rather than `Reject::Malformed`.
- In `is_malformed_from_verification`, remove the `InternalErrorKind::CapacityOverflow` branch or reclassify it as non-malformed (e.g., return `false`), since arithmetic overflow on semantically valid field values is not structural malformation.
- Add a per-output capacity upper-bound check in `NonContextualTransactionVerifier` (e.g., reject any single output with capacity > total CKB supply) to catch this before resolve.

### Proof of Concept
```
outputs[0].capacity = 9_223_372_036_854_775_808  // u64::MAX/2 + 1
outputs[1].capacity = 9_223_372_036_854_775_808  // u64::MAX/2 + 1
inputs[0] = <any live UTXO from chain>
```
Relay via `RelayTransaction` P2P message. Assert: peer bans the sender's IP within seconds; no relay propagation occurs; DB read count = 1 (resolve). [4](#0-3) [8](#0-7) [5](#0-4) [9](#0-8)

### Citations

**File:** verification/src/transaction_verifier.rs (L93-102)
```rust
    /// Perform context-independent verification
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
    }
```

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
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

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/occupied-capacity/core/src/units.rs (L125-130)
```rust
    pub fn safe_add<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_add(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
    }
```

**File:** tx-pool/src/util.rs (L34-41)
```rust
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
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

**File:** error/src/convert.rs (L6-10)
```rust
impl_error_conversion_with_kind!(
    ckb_occupied_capacity::Error,
    InternalErrorKind::CapacityOverflow,
    InternalError
);
```
