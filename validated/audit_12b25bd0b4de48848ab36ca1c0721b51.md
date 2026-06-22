### Title
Permissionless Orphan-Pool Slot Exhaustion Enables Continuous Eviction of Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

---

### Summary

The CKB tx-pool `OrphanPool` has a hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries. Any unprivileged P2P peer can relay structurally valid transactions that reference non-existent inputs, causing them to be admitted to the orphan pool with no fee-rate check and no per-peer quota. When the pool is full, eviction is random with no priority ordering. A single malicious peer can continuously saturate the orphan pool with 100 zero-cost junk entries, causing legitimate orphan transactions from honest peers to be randomly evicted and permanently marked as "unknown" in the relay filter, disrupting transaction propagation.

---

### Finding Description

**Root cause — `tx-pool/src/component/orphan.rs`**

The `OrphanPool` enforces a global cap:

```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
``` [1](#0-0) 

When the pool is full, `limit_size()` first evicts expired entries, then evicts **randomly** until the count is at or below the cap:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
``` [2](#0-1) 

`add_orphan_tx` accepts any transaction from any peer with no per-peer quota and no fee-rate check:

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
    ...
    // DoS prevention: do not allow OrphanPool to grow unbounded
    self.limit_size()
}
``` [3](#0-2) 

**Why fee-rate checks are bypassed for orphan transactions:**

A transaction enters the orphan pool only after failing `pre_check` with a missing-input error. Because inputs cannot be resolved, the fee cannot be computed, so `min_fee_rate` enforcement never applies. The path in `after_process` is:

```rust
if is_missing_input(reject) {
    self.send_result_to_relayer(TxVerificationResult::UnknownParents { ... });
    self.add_orphan(tx, peer, declared_cycle).await;
}
``` [4](#0-3) 

**Eviction consequence:**

Every evicted orphan hash is sent back to the relayer as a `Reject` result, which marks it as "unknown" in the bloom filter. This means the node will not re-request the transaction from peers, permanently suppressing it from the local pool until the parent arrives and triggers a re-relay — which may never happen if the parent itself was also disrupted. [5](#0-4) 

**Expiry window:**

Orphan entries expire after `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL = 100 * 48 = 4800 seconds` (~80 minutes). An attacker who continuously re-floods the pool can maintain saturation indefinitely. [6](#0-5) 

---

### Impact Explanation

A malicious P2P peer floods the `OrphanPool` with 100 structurally valid CKB transactions referencing non-existent `OutPoint`s. These cost the attacker nothing (no CKB tokens, no PoW). The pool stays saturated. Every legitimate orphan transaction relayed by honest peers is randomly evicted and marked as "unknown," breaking the two-phase transaction relay flow for any transaction whose parent is in-flight. This degrades transaction propagation across the network and can cause legitimate transactions to be silently dropped from the mempool pipeline.

---

### Likelihood Explanation

The attack is reachable by any unprivileged P2P peer via the `RelayV3` protocol. No keys, no tokens, no privileged access are required. The attacker only needs to craft 100 minimal valid CKB transactions (correct molecule serialization, referencing fake `OutPoint`s). The `TooManyUnknownTransactions` ban applies only to `GetRelayTransactions` hash-announcement messages, not to relaying actual transaction data, so the attacker is not banned for this behavior. [7](#0-6) 

The attack is cheap, repeatable, and requires no coordination.

---

### Recommendation

1. **Per-peer orphan quota**: Track how many orphan entries each `PeerIndex` has contributed. Evict the peer's own entries first when the pool is full, and cap the number of orphan entries any single peer can hold (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / 4`).

2. **Priority-based eviction**: Instead of random eviction, evict the entry with the lowest declared cycle count (a proxy for fee-rate) or the oldest entry, making it harder for zero-cost junk to displace high-value orphans.

3. **Structural cost floor**: Require a minimum declared cycle count for orphan admission, making each junk entry slightly more expensive to produce.

---

### Proof of Concept

An attacker peer connects via `RelayV3` and sends 100 `RelayTransaction` messages, each containing a valid CKB transaction that spends a non-existent `OutPoint` (e.g., `OutPoint::new(random_hash, 0)`). Each transaction passes molecule deserialization and basic structural checks, then fails at input resolution with `OutPointError::Unknown`, triggering `is_missing_input` → `add_orphan`. After 100 such messages, `OrphanPool::len() == 100`. Any subsequent legitimate orphan transaction from an honest peer causes `limit_size()` to randomly evict one existing entry. The attacker continuously re-sends evicted entries to maintain saturation. Legitimate orphan transactions are continuously displaced and marked as rejected in the relay filter. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
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

**File:** tx-pool/src/component/orphan.rs (L134-158)
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

**File:** test/src/specs/relay/too_many_unknown_transactions.rs (L1-46)
```rust
use crate::util::cell::gen_spendable;
use crate::util::transaction::always_success_transaction;
use crate::utils::{build_relay_tx_hashes, since_from_absolute_timestamp, wait_until};
use crate::{Net, Node, Spec};
use ckb_constant::sync::{MAX_RELAY_TXS_NUM_PER_BATCH, MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER};
use ckb_network::SupportProtocols;
use ckb_types::packed::CellInput;

pub struct TooManyUnknownTransactions;

impl Spec for TooManyUnknownTransactions {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];
        let mut net = Net::new(
            self.name(),
            node0.consensus(),
            vec![SupportProtocols::Sync, SupportProtocols::RelayV3],
        );
        net.connect(node0);

        // Send `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` transactions with a same input
        let input = gen_spendable(node0, 1)[0].to_owned();
        let tx_template = always_success_transaction(node0, &input);
        let txs = {
            (0..MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER).map(|i| {
                let since = since_from_absolute_timestamp(i as u64);
                tx_template
                    .as_advanced_builder()
                    .set_inputs(vec![CellInput::new(input.out_point.clone(), since)])
                    .build()
            })
        };
        let tx_hashes = txs.map(|tx| tx.hash()).collect::<Vec<_>>();
        assert!(MAX_RELAY_TXS_NUM_PER_BATCH >= tx_hashes.len());
        net.send(
            node0,
            SupportProtocols::RelayV3,
            build_relay_tx_hashes(&tx_hashes),
        );

        let banned = wait_until(60, || node0.rpc_client().get_banned_addresses().len() == 1);
        assert!(
            banned,
            "NetController should be banned cause TooManyUnknownTransactions"
        );
    }
```
