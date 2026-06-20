### Title
Incomplete DoS Prevention in OrphanPool: Fee-Rate Check Bypassed for Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

---

### Summary

The CKB tx-pool enforces a `min_fee_rate` threshold as its primary DoS prevention mechanism for all submitted transactions. However, this check is structurally impossible to apply to orphan transactions (those with unresolvable inputs), and the `OrphanPool` has no compensating control. Any connected P2P peer can flood the orphan pool with zero-fee transactions, evicting legitimate orphan transactions via random replacement, with no per-peer limit and no fee-rate gate.

---

### Finding Description

The tx-pool's DoS prevention has two layers:

1. **Main pool**: `check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` by computing `fee = inputs_capacity - outputs_capacity` against a resolved `ResolvedTransaction`. This is called from `pre_check` in `tx-pool/src/process.rs`.

2. **Orphan pool**: `OrphanPool.add_orphan_tx` in `tx-pool/src/component/orphan.rs` accepts any transaction whose inputs cannot be resolved (unknown parent outputs). The only admission control is a global count cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`.

The structural gap is that `check_tx_fee` requires a fully resolved transaction. When `pre_check` calls `resolve_tx` and the inputs are unknown, it returns `Err(Reject::Resolve(OutPointError::Unknown))` before `check_tx_fee` is ever reached. The transaction is then routed to `add_orphan_tx`, which performs no fee-rate check:

```rust
// tx-pool/src/component/orphan.rs
pub fn add_orphan_tx(
    &mut self,
    tx: TransactionView,
    peer: PeerIndex,
    declared_cycle: Cycle,
) -> Vec<Byte32> {
    if self.entries.contains_key(&tx.proposal_short_id()) {
        return vec![];
    }
    // ← no fee-rate check here
    self.entries.insert(tx.proposal_short_id(), Entry::new(tx.clone(), peer, declared_cycle));
    for out_point in tx.input_pts_iter() {
        self.by_out_point.entry(out_point).or_default().insert(tx.proposal_short_id());
    }
    // DoS prevention: do not allow OrphanPool to grow unbounded
    self.limit_size()
}
```

The `limit_size()` function evicts entries randomly (via `HashMap::keys().next()`) once the pool exceeds 100 entries:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
```

There is no per-peer quota. A single attacker peer can occupy all 100 slots.

The `non_contextual_verify` path (the only gate before orphan admission) only checks structural validity and the 512 KB size limit — it does not check fee rate:

```rust
// tx-pool/src/util.rs
pub(crate) fn non_contextual_verify(consensus: &Consensus, tx: &TransactionView) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus).verify().map_err(Reject::Verification)?;
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(tx_size, TRANSACTION_SIZE_LIMIT));
    }
    if tx.is_cellbase() { ... }
    Ok(())
}
```

---

### Impact Explanation

An attacker connected as a P2P peer can:

1. Craft up to 100 structurally valid transactions whose inputs reference non-existent (or not-yet-confirmed) outputs, with zero fee (outputs capacity = inputs capacity).
2. Relay them via the standard `RelayTransactions` P2P message.
3. Each passes `non_contextual_verify`, enters the verify queue, fails `resolve_tx` with `OutPointError::Unknown`, and is admitted to the orphan pool.
4. Once the pool reaches 100 entries, each new attacker transaction randomly evicts a legitimate orphan.
5. Legitimate users whose transactions arrive out-of-order (a normal condition during high-throughput relay) have their orphan transactions silently dropped.

The protection mechanism (`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`) exists but has incomplete coverage: it bounds total memory but does not prevent a single peer from monopolizing all slots with zero-cost transactions, bypassing the `min_fee_rate` DoS gate that protects the main pool.

---

### Likelihood Explanation

- **Entry path**: Any unauthenticated P2P peer. No special role, key, or privilege required.
- **Cost**: Zero fee per transaction. The attacker only needs to construct valid transaction structures with fabricated input `OutPoint`s.
- **Detectability**: The attack is silent — evicted orphans are not surfaced as errors to the original submitter; they simply disappear from the pool.
- **Repeatability**: The attacker can continuously re-fill the pool after each eviction cycle.

---

### Recommendation

Apply a compensating control at the orphan pool admission boundary. Options include:

1. **Per-peer orphan quota**: Track orphan count per `PeerIndex` and reject new orphans from a peer that already holds `N` slots (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`).
2. **Declared-fee heuristic**: Require the submitter to declare a minimum fee (analogous to the declared-cycles mechanism already present in `Entry.cycle`), and reject orphans whose declared fee is below `min_fee_rate * tx_size`.
3. **Evict by peer contribution**: In `limit_size`, prefer evicting entries from the peer with the most orphan slots rather than evicting randomly.

---

### Proof of Concept

```
Attacker peer connects to a CKB node.

For i in 0..100:
    tx_i = Transaction {
        inputs:  [CellInput { previous_output: OutPoint { tx_hash: random_hash_i, index: 0 }, since: 0 }],
        outputs: [CellOutput { capacity: 100_CKB, lock: always_success }],
        // outputs_capacity == inputs_capacity → zero fee
        witnesses: [...]
    }
    send RelayTransactions([tx_i]) to node

Result:
- All 100 tx_i pass non_contextual_verify (valid structure, size < 512KB)
- All 100 fail resolve_tx with OutPointError::Unknown (inputs don't exist)
- All 100 are admitted to OrphanPool (no fee check)
- OrphanPool is now full (100/100 slots held by attacker)
- Any legitimate orphan tx submitted by honest peers is randomly evicted
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/component/orphan.rs (L14-16)
```rust
/// 100 max block interval
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L96-132)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
    }
```

**File:** tx-pool/src/component/orphan.rs (L134-159)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
    }
```

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
```

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
}
```

**File:** tx-pool/src/process.rs (L318-333)
```rust
    pub(crate) async fn non_contextual_verify(
        &self,
        tx: &TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
    }
```

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L557-573)
```rust
    pub(crate) async fn add_orphan(
        &self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) {
        let evicted_txs = self
            .orphan
            .write()
            .await
            .add_orphan_tx(tx, peer, declared_cycle);
        // for any evicted orphan tx, we should send reject to relayer
        // so that we mark it as `unknown` in filter
        for tx_hash in evicted_txs {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }
    }
```
