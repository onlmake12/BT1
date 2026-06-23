### Title
Tx-Pool Eviction DoS: Attacker Can Silently Evict Legitimate Transactions via Timestamp-Ordered `limit_size` Eviction — (File: tx-pool/src/pool.rs)

---

### Summary

The CKB tx-pool's `limit_size` function evicts the **oldest** transaction with the **lowest fee rate** when the pool exceeds `max_tx_pool_size`. An unprivileged attacker who submits transactions **after** a victim's transaction — at the same or higher fee rate — can cause the victim's (older, lower-priority) transaction to be silently evicted from the pool. The victim already received a success response from `send_transaction` and has no immediate notification of the eviction.

---

### Finding Description

**Root cause — `limit_size` in `tx-pool/src/pool.rs`:** [1](#0-0) 

After every successful insertion via `submit_entry`, `limit_size` is called: [2](#0-1) 

The eviction candidate is selected by `next_evict_entry`, which iterates the pool in ascending `EvictKey` order: [3](#0-2) 

`EvictKey` is ordered as follows (ascending = evicted first):

```
fee_rate ASC → descendants_count ASC → timestamp ASC
``` [4](#0-3) 

The `timestamp` field is set to `unix_time_as_millis()` at insertion time: [5](#0-4) 

**Consequence:** Among transactions with equal fee rate and equal descendants count, the **oldest** (lowest timestamp) is evicted first. A transaction submitted before the attacker's transactions is therefore the first candidate for eviction.

**Attack path:**

1. Victim submits transaction `Tx_V` at fee rate `F` (timestamp `T_v`). The async `send_transaction` RPC returns a hash — success.
2. Attacker observes `Tx_V` in the pool and submits many transactions at fee rate `F` (or `F+ε`) with timestamps `T_a > T_v`.
3. As each attacker transaction is inserted and `limit_size` fires, the pool is over `max_tx_pool_size`. The eviction loop selects the entry with the smallest `EvictKey` — `Tx_V` (oldest, lowest or equal fee rate).
4. `Tx_V` is removed via `callbacks.call_reject`. The attacker's newly inserted transaction is **not** the `current_entry_id`, so `limit_size` returns `None` for the attacker — the attacker's transaction stays.
5. The victim's transaction is silently gone. The victim only discovers this by polling `get_transaction` and seeing a `Rejected` status.

The test `test_pool_evict` confirms the eviction order: with equal fees, the entry with the earliest timestamp is evicted first: [6](#0-5) 

**Secondary vector — zero-fee spam when `min_fee_rate = 0`:**

When a node is configured with `min_fee_rate = FeeRate::zero()` (a valid and tested configuration), an attacker can submit minimum-size transactions with zero fee to fill the 180 MB pool at negligible cost, then use the timestamp-ordering attack above to evict any victim transaction: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

A victim's transaction is silently evicted from the tx-pool after the victim already received a success response. The transaction will never be proposed or committed. The victim must resubmit, and the attacker can repeat the eviction. This constitutes a targeted, repeatable DoS against specific transaction senders. With `min_fee_rate = 0`, the attack is essentially free; with the default 1000 shannons/KW, it requires paying fees but remains feasible given the 180 MB pool capacity.

---

### Likelihood Explanation

- The attacker entry path is fully unprivileged: any RPC caller or P2P peer can submit transactions.
- The pool is publicly observable via `get_tip_tx_pool_info` and `get_raw_tx_pool`, so the attacker can monitor when the victim's transaction enters.
- The attack is amplified when `min_fee_rate = 0` (zero cost per spam transaction).
- Even at default `min_fee_rate = 1000`, the pool holds ~180 MB of transactions; an attacker with sufficient UTXOs can fill it.
- The victim has no real-time notification of eviction.

---

### Recommendation

1. **Pre-admission pool-fullness check**: Before inserting a transaction, compare its fee rate against the current lowest-fee-rate entry in the pool. If the pool is full and the incoming transaction's fee rate is not strictly higher than the lowest existing entry, reject it immediately with `Reject::Full` rather than inserting and then evicting.

2. **Evict-before-insert**: Evict the lowest-priority entry *before* inserting the new one, and only proceed with insertion if the new transaction's fee rate is strictly greater than the evicted entry's fee rate. This prevents a lower-fee transaction from displacing a higher-fee one.

3. **Minimum fee rate enforcement**: Ensure `min_fee_rate` is never set to zero in production deployments, or enforce a protocol-level minimum.

---

### Proof of Concept

```
1. Configure a node with min_fee_rate = 0 and max_tx_pool_size = 2000 (as in SizeLimit test).
2. Fill the pool to near capacity with transactions T_a1..T_an (fee=0, timestamps T_a).
3. Submit victim transaction T_v (fee=0, timestamp T_v < T_a for any future attacker tx).
4. Submit one more attacker transaction T_a_last (fee=0, timestamp T_a_last > T_v).
5. limit_size fires: pool is over max_tx_pool_size.
6. next_evict_entry returns T_v (oldest, same fee_rate=0, descendants_count=1).
7. T_v is evicted via call_reject; T_a_last remains.
8. Victim polls get_transaction(T_v.hash()) → status: Rejected.
```

The `SizeLimit` integration test in `test/src/specs/tx_pool/limit.rs` already demonstrates the pool-full eviction path with `min_fee_rate = FeeRate::zero()`, confirming the mechanism is reachable. [9](#0-8)

### Citations

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```

**File:** tx-pool/src/process.rs (L150-152)
```rust
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;
```

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```

**File:** tx-pool/src/component/sort_key.rs (L92-103)
```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
```

**File:** tx-pool/src/component/entry.rs (L48-50)
```rust
    pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
        Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
    }
```

**File:** tx-pool/src/component/tests/pending.rs (L278-313)
```rust
fn test_pool_evict() {
    let mut pool = PoolMap::new(1000);
    let tx1 = build_tx(vec![(&Byte32::zero(), 1), (&h256!("0x1").into(), 1)], 1);
    let tx2 = build_tx(
        vec![(&h256!("0x2").into(), 1), (&h256!("0x3").into(), 1)],
        3,
    );
    let tx3 = build_tx_with_dep(
        vec![(&h256!("0x4").into(), 1)],
        vec![(&h256!("0x5").into(), 1)],
        3,
    );
    let entry1 = TxEntry::dummy_resolve(tx1.clone(), MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    std::thread::sleep(Duration::from_millis(1));
    let entry2 = TxEntry::dummy_resolve(tx2.clone(), MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    std::thread::sleep(Duration::from_millis(1));
    let entry3 = TxEntry::dummy_resolve(tx3.clone(), MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);

    assert!(pool.add_entry(entry1, Status::Pending).is_ok());
    assert!(pool.add_entry(entry2, Status::Pending).is_ok());
    assert!(pool.add_entry(entry3, Status::Pending).is_ok());

    let e1 = pool.next_evict_entry(Status::Pending).unwrap();
    assert_eq!(e1, tx1.proposal_short_id());
    pool.remove_entry(&e1);

    let e2 = pool.next_evict_entry(Status::Pending).unwrap();
    assert_eq!(e2, tx2.proposal_short_id());
    pool.remove_entry(&e2);

    let e3 = pool.next_evict_entry(Status::Pending).unwrap();
    assert_eq!(e3, tx3.proposal_short_id());
    pool.remove_entry(&e3);

    assert!(pool.next_evict_entry(Status::Pending).is_none());
}
```

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

**File:** test/src/specs/tx_pool/limit.rs (L19-67)
```rust
impl Spec for SizeLimit {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node = &nodes[0];

        info!("Generate DEFAULT_TX_PROPOSAL_WINDOW block on node");
        node.mine_until_out_bootstrap_period();

        info!("Generate 1 tx on node");
        let mut txs_hash = Vec::new();
        let tx = node.new_transaction_spend_tip_cellbase();
        let mut hash = node.submit_transaction(&tx);
        txs_hash.push(hash.clone());

        let tx_pool_info = node.get_tip_tx_pool_info();
        let one_tx_size = tx_pool_info.total_tx_size.value();
        let one_tx_cycles = tx_pool_info.total_tx_cycles.value();

        info!(
            "one_tx_cycles: {}, one_tx_size: {}",
            one_tx_cycles, one_tx_size
        );

        assert!(MAX_MEM_SIZE_FOR_SIZE_LIMIT as u64 > one_tx_size * 2);

        let max_tx_num = (MAX_MEM_SIZE_FOR_SIZE_LIMIT as u64) / one_tx_size;

        info!("Generate as much as possible txs on : {}", max_tx_num);
        (0..(max_tx_num - 1)).for_each(|_| {
            let tx = node.new_transaction(hash.clone());
            hash = node.rpc_client().send_transaction(tx.data().into());
            txs_hash.push(hash.clone());
            sleep(Duration::from_millis(10));
        });

        info!("The next tx reach size limit");
        let _tx = node.new_transaction(hash);
        node.assert_tx_pool_serialized_size((max_tx_num) * one_tx_size);
        let last =
            node.mine_with_blocking(|template| template.proposals.len() != max_tx_num as usize);
        node.assert_tx_pool_serialized_size(max_tx_num * one_tx_size);
        node.mine_with_blocking(|template| template.number.value() != (last + 1));
        node.mine_with_blocking(|template| template.transactions.len() != max_tx_num as usize);
        node.assert_tx_pool_serialized_size(0);
    }

    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        config.tx_pool.max_tx_pool_size = MAX_MEM_SIZE_FOR_SIZE_LIMIT;
        config.tx_pool.min_fee_rate = FeeRate::zero();
    }
```
