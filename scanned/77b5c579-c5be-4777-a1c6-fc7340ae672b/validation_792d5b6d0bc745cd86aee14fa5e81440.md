### Title
Orphan Pool Flooding via P2P Relay Enables Griefing of Legitimate Child Transactions - (File: `tx-pool/src/component/orphan.rs`)

### Summary

An unprivileged P2P peer can flood the `OrphanPool` with fake orphan transactions at near-zero cost, causing legitimate child transactions to be randomly evicted. When the legitimate parent transaction later arrives and is accepted, `process_orphan_tx` finds no children in the pool, silently dropping the child. The victim must resubmit. The attacker can sustain this indefinitely.

### Finding Description

`OrphanPool` in `tx-pool/src/component/orphan.rs` enforces a hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. [1](#0-0) 

When the pool exceeds this limit, `limit_size()` evicts **one random entry** by taking the first key from a `HashMap` (non-deterministic iteration order): [2](#0-1) 

`add_orphan_tx` enforces **no per-peer quota**. The `peer: PeerIndex` field is stored in each `Entry` but is never consulted during eviction: [3](#0-2) 

The orphan pool is populated via the P2P relay path. In `after_process`, whenever a remote transaction fails with a missing-input error, it is unconditionally added to the orphan pool: [4](#0-3) 

The relay entry point in `TransactionsProcess::execute` only checks that declared cycles do not exceed `max_block_cycles` before forwarding to `submit_remote_tx`: [5](#0-4) 

An attacker who is a connected P2P peer can:

1. Craft 100 distinct transactions `T1…T100`, each referencing non-existent parent tx hashes as inputs (so they will always be classified as orphans).
2. Announce their hashes via `RelayTransactionHashes`. The victim node sends `GetRelayTransactions` for each.
3. Respond with `T1…T100`. Each passes the declared-cycles check and is forwarded to `submit_remote_tx → after_process → add_orphan`.
4. The orphan pool is now full (100 entries, all attacker-controlled).
5. When a legitimate child transaction `Tc` (whose parent `Tp` is in transit) arrives from another peer, it is inserted, bringing the pool to 101. `limit_size` evicts one random entry — with 100 attacker entries and 1 legitimate entry, `Tc` has a 1/101 chance of being evicted per round.
6. The attacker immediately sends a replacement fake orphan to keep the pool at 100. Repeating this, `Tc` is evicted within an expected ~101 rounds.
7. When `Tp` is later accepted, `process_orphan_tx` calls `find_by_previous`, which looks up children by out-point in `by_out_point`. Since `Tc` was evicted, it is not found and is silently dropped. [6](#0-5) 

The orphan expiry time is `100 * MAX_BLOCK_INTERVAL` (≈50 minutes), so attacker entries persist long enough to sustain the attack without constant refreshing. [1](#0-0) 

### Impact Explanation

Legitimate child transactions that depend on in-flight parents are silently evicted from the orphan pool. The parent's acceptance no longer triggers automatic child processing. The child must be resubmitted by the user. An attacker sustaining the flood can block all orphan-dependent transaction chains on a targeted node indefinitely, causing a persistent denial-of-service for any workflow that relies on the parent→child relay ordering (e.g., multi-hop transaction chains, layer-2 commitment flows).

### Likelihood Explanation

Any peer that can establish a P2P connection to the node can execute this attack. The cost is network bandwidth only — no on-chain fees are required because orphan transactions are stored before script verification. The pool limit of 100 is small enough that a single peer can fill it in one round-trip. The attack is repeatable and self-sustaining.

### Recommendation

1. **Per-peer orphan quota**: Track how many orphan entries each `PeerIndex` has contributed. When the pool is full, evict from the peer with the most entries rather than randomly.
2. **Increase pool size**: Raise `DEFAULT_MAX_ORPHAN_TRANSACTIONS` to reduce the probability that a single attacker can dominate the pool.
3. **Evict attacker entries on ban**: When a peer is banned (e.g., for declaring wrong cycles), remove all orphan entries attributed to that peer. The `peer` field already stored in `Entry` makes this straightforward.
4. **Rate-limit orphan acceptance per peer**: Reject orphan submissions from a peer that already has N entries in the pool.

### Proof of Concept

```
1. Connect to a CKB node as a P2P peer using the Tentacle/RelayV3 protocol.

2. Construct 100 transactions T1…T100:
   - Each Ti has one input referencing a random, non-existent tx hash
     (e.g., OutPoint { tx_hash: random_bytes(32), index: 0 })
   - Each Ti has one output with minimal capacity
   - No witness needed (non-contextual checks pass; script verification
     is deferred until the parent is resolved, which never happens)

3. Send RelayTransactionHashes{ hashes: [hash(T1), …, hash(T100)] }
   The node responds with GetRelayTransactions for each hash.

4. Respond with RelayTransactions containing T1…T100, each with
   declared_cycles = 1 (well below max_block_cycles).

5. Each Ti fails resolve with OutPointError::Unknown (missing input),
   triggering add_orphan in after_process. The orphan pool is now full.

6. A legitimate user relays child transaction Tc (parent Tp is in transit).
   Tc is added → pool size = 101 → limit_size evicts one random entry.
   Attacker immediately sends T101 (a new fake orphan) to refill the pool.

7. Repeat step 6 until Tc is evicted (~101 iterations on average).

8. Relay Tp to the node. Tp is accepted. process_orphan_tx finds no
   children for Tp's outputs. Tc is silently lost.

Observable result: tx_pool_info.orphan stays at 100 (all attacker-owned);
Tc never appears in pending after Tp is committed.
``` [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/component/orphan.rs (L161-167)
```rust
    pub fn find_by_previous(&self, tx: &TransactionView) -> Vec<&ProposalShortId> {
        tx.output_pts()
            .iter()
            .filter_map(|out_point| self.by_out_point.get(out_point))
            .flatten()
            .collect::<Vec<_>>()
    }
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
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

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
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
```
