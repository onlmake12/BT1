### Title
Missing Minimum `declared_cycles` Validation Allows Attacker to Permanently Suppress Transaction Relay Until TTL Expiry — (`sync/src/relayer/transactions_process.rs`)

---

### Summary

An unprivileged P2P peer can relay a valid transaction with an artificially low `declared_cycles` value (e.g., `1`). The node marks the transaction hash as "known" in `tx_filter` **before** verification completes. When script verification then fails with `ExceededMaximumCycles` (treated as a malformed-tx rejection), `is_allowed_relay()` returns `false`, so `remove_from_known_txs` is never called. The transaction hash remains stuck in `tx_filter` until its TTL expires, preventing any other peer from relaying the same transaction to this node during that window.

---

### Finding Description

**Entry path — `TransactionsProcess::execute()`**

When a `RelayTransactions` P2P message arrives, `TransactionsProcess::execute()` performs only an **upper-bound** check on `declared_cycles`:

```
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_block_cycles) { ban_peer(...) }
```

There is **no lower-bound check**. A value of `1` (or any value below the actual script cost) passes this guard. [1](#0-0) 

Immediately after the guard, the tx hash is **optimistically marked as known** in `tx_filter` and removed from `unknown_tx_hashes`: [2](#0-1) 

The transaction is then submitted to the pool via `submit_remote_tx`, which calls `_process_tx`. Inside `_process_tx`, the attacker-supplied `declared_cycles` is used directly as `max_cycles` for script verification: [3](#0-2) 

With `declared_cycles = 1`, the CKB-VM immediately exhausts the cycle budget and returns `ScriptError::ExceededMaximumCycles`, which propagates as `Reject::Verification(err)`. The `DeclaredWrongCycles` branch is **never reached** because `verify_rtx` returns an error before the cycle-comparison check: [4](#0-3) 

**Why the tx hash is never cleaned up**

`is_malformed_from_verification` classifies any `ErrorKind::Script` error (except `ARGV_TOO_LONG`) as malformed: [5](#0-4) 

`ExceededMaximumCycles` is a `ScriptError`, so `is_malformed_tx()` returns `true`. Consequently, `is_allowed_relay()` returns `false`: [6](#0-5) 

In `after_process`, because `is_allowed_relay()` is `false`, `TxVerificationResult::Reject { tx_hash }` is **never sent** to the relayer: [7](#0-6) 

Without that signal, `remove_from_known_txs` is never called, so the tx hash remains in `tx_filter`: [8](#0-7) 

**Consequence for subsequent peers**

When any other peer later announces the same tx hash via `RelayTransactionHashes`, `TransactionHashesProcess::execute()` filters it out because it is already in `tx_filter`: [9](#0-8) 

The node never adds the hash to `unknown_tx_hashes`, never sends `GetRelayTransactions`, and never receives the transaction — until the TTL entry expires.

---

### Impact Explanation

An attacker can suppress propagation of any specific transaction to a targeted node for the duration of the `tx_filter` TTL. By cycling through IP addresses (the ban duration is 3 days per IP but the attacker only needs one connection per attack), the attacker can keep a transaction blocked across multiple TTL windows. This delays or prevents transaction confirmation for the victim node, and in a targeted attack against miners or relay hubs, can degrade network-wide transaction propagation.

---

### Likelihood Explanation

The attack requires only a single P2P connection and one `RelayTransactions` message with `declared_cycles = 1`. No funds, no privileged access, and no cryptographic material are needed. The attacker is banned per IP, but the cost of rotating IPs is negligible. Any unprivileged peer on the network can execute this.

---

### Recommendation

1. **Add a minimum `declared_cycles` check** in `TransactionsProcess::execute()`, analogous to the existing maximum check. Reject (and ban) peers that declare cycles below a protocol-defined floor (e.g., the minimum cycles required for a valid lock script). [1](#0-0) 

2. **Clean up `tx_filter` on `ExceededMaximumCycles` rejection.** When verification fails because `declared_cycles` was too low (distinguishable from a genuinely invalid script by comparing declared vs. actual cycles), treat it like `DeclaredWrongCycles`: call `remove_from_known_txs` so the transaction can be re-requested from other peers with the correct cycle count. [10](#0-9) 

---

### Proof of Concept

1. Attacker connects to a CKB node as a P2P peer.
2. Attacker sends `RelayTransactionHashes` containing the hash of a valid pending transaction `T`.
3. Node responds with `GetRelayTransactions` for `T`.
4. Attacker sends `RelayTransactions` with `T` and `cycles = 1`.
5. Node executes `TransactionsProcess::execute()`:
   - `1 <= max_block_cycles` → passes the upper-bound guard.
   - `mark_as_known_txs([T.hash()])` → `T.hash()` inserted into `tx_filter`, removed from `unknown_tx_hashes`.
   - `submit_remote_tx(T, declared_cycles=1, peer)` → `_process_tx` runs script verification with `max_cycles=1` → `ExceededMaximumCycles` → `Reject::Verification` → `ban_malformed(peer)`.
   - `is_allowed_relay()` is `false` → `TxVerificationResult::Reject` not sent → `remove_from_known_txs` never called.
6. Attacker is banned. `T.hash()` remains in `tx_filter`.
7. Legitimate peer B announces `T.hash()` → filtered by `tx_filter` → node never requests `T` from peer B.
8. Transaction `T` is suppressed on this node until TTL expiry. [11](#0-10) [12](#0-11)

### Citations

**File:** sync/src/relayer/transactions_process.rs (L37-96)
```rust
    pub fn execute(self) -> Status {
        let shared_state = self.relayer.shared().state();
        let txs: Vec<(TransactionView, Cycle)> = {
            // ignore the tx if it's already known or it has never been requested before
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
            let unknown_tx_hashes = shared_state.unknown_tx_hashes();

            self.message
                .transactions()
                .iter()
                .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
        };

        if txs.is_empty() {
            return Status::ok();
        }

        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }

        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

        let tx_pool = self.relayer.shared.shared().tx_pool_controller().clone();
        let peer = self.peer;
        self.relayer
            .shared
            .shared()
            .async_handle()
            .spawn(async move {
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });

        Status::ok()
    }
```

**File:** tx-pool/src/process.rs (L514-521)
```rust
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
```

**File:** tx-pool/src/process.rs (L705-749)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
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

**File:** util/types/src/core/tx_pool.rs (L87-97)
```rust
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

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-49)
```rust
        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };

        state.add_ask_for_txs(self.peer, tx_hashes)
```
