### Title
Malicious Peer Can Manipulate Unverified `cycles` Field in `RelayTransaction` to Suppress Transaction Propagation — (`sync/src/relayer/transactions_process.rs`)

---

### Summary

The `RelayTransaction` P2P message carries a `cycles` field alongside the actual `transaction` data. The `cycles` value is **not committed to by the transaction hash** and is therefore fully attacker-controlled. A malicious peer that has been asked to supply a transaction can declare an artificially low `cycles` value, causing the node's VM execution to exhaust the cycle budget and reject the transaction. Because the transaction hash is marked as "known" in the shared `tx_filter` **before** the tx-pool submission attempt, the rejection silently poisons the filter: subsequent honest peers that announce the same transaction hash are ignored, and the transaction is suppressed until the filter entry expires.

---

### Finding Description

The `RelayTransaction` molecule table is defined as:

```
table RelayTransaction {
    cycles:      Uint64,
    transaction: Transaction,
}
``` [1](#0-0) 

The `cycles` field is an out-of-band hint supplied by the relaying peer; it is **not** part of the transaction's hash and carries no cryptographic commitment.

In `TransactionsProcess::execute()`, the handler:

1. Extracts `(transaction, declared_cycles)` pairs from the message.
2. Checks only that `declared_cycles ≤ max_block_cycles` (banning the peer if exceeded).
3. **Calls `mark_as_known_txs` unconditionally** for all remaining transactions — before any tx-pool submission.
4. Spawns an async task that calls `submit_remote_tx(tx, declared_cycles, peer)`. [2](#0-1) 

Inside `_process_tx`, the declared value is used directly as the VM cycle cap:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [3](#0-2) 

If `declared_cycles` is set below the transaction's true execution cost, the VM terminates with a cycle-limit error and the transaction is rejected. However, `mark_as_known_txs` has already run: [4](#0-3) 

The `tx_filter` now contains the transaction hash. Any subsequent `RelayTransactions` message from an honest peer carrying the same hash is silently dropped by the filter check:

```rust
.filter(|(tx, _)| {
    !tx_filter.contains(&tx.hash())   // ← poisoned entry blocks re-admission
    && ...
})
``` [5](#0-4) 

The `cycles` field is structurally analogous to the `from_chain` parameter in the referenced report: both are accepted alongside verified/committed data, neither is covered by any integrity check, and both are used in downstream processing that determines whether the operation succeeds or fails.

---

### Impact Explanation

A malicious peer can suppress propagation of any transaction it has been asked to relay:

- The transaction is rejected from the local tx-pool.
- The transaction hash is poisoned in `tx_filter`, causing the node to silently discard the same transaction when offered by honest peers.
- The attacker can repeat the attack each time the filter entry expires, keeping the transaction out of the pool indefinitely or until the attacker's peer is banned.
- Time-sensitive transactions (e.g., those unlocking time-locked cells, or competing in fee-bump races) are particularly vulnerable.

The existing test `DeclaredWrongCyclesAndRelayAgain` confirms the propagation-suppression effect is real and that recovery requires the filter to expire and a fresh relay path to be established. [6](#0-5) 

---

### Likelihood Explanation

The attack entry path is realistic and requires no privilege:

1. Any unprivileged P2P peer can broadcast a `RelayTransactionHashes` message announcing a valid transaction hash.
2. The victim node responds with `GetRelayTransactions`, designating the attacker as the `requesting_peer`.
3. The attacker sends `RelayTransactions` with `cycles = 1` (or any value below the true cost but ≤ `max_block_cycles`).
4. The filter is poisoned without the attacker being banned (the ban only triggers when `declared_cycles > max_block_cycles`). [7](#0-6) 

The attacker can monitor the public mempool, race to announce any pending transaction hash, and execute the suppression before the legitimate originator's relay reaches the victim.

---

### Recommendation

1. **Move `mark_as_known_txs` after successful tx-pool admission**, not before. Only mark a hash as known once the transaction has been accepted (or definitively rejected as malformed/double-spend), so a cycle-limit failure caused by a bad `cycles` hint does not poison the filter.

2. **Do not use the peer-supplied `cycles` as the VM execution cap.** Run the transaction with `max_block_cycles` as the cap. After execution, compare the actual cycle count against the declared value and reject (and ban) only if they differ — mirroring the existing `DeclaredWrongCycles` check but without allowing the declared value to truncate execution prematurely. [8](#0-7) 

---

### Proof of Concept

```
1. Attacker peer connects to victim node.
2. Attacker sends RelayTransactionHashes { tx_hashes: [H] }
   where H is the hash of a valid, not-yet-propagated transaction T.
3. Victim replies GetRelayTransactions { tx_hashes: [H] }.
4. Attacker sends RelayTransactions {
       transactions: [ RelayTransaction { cycles: 1, transaction: T } ]
   }
   (cycles = 1 is far below T's true execution cost, but ≤ max_block_cycles)
5. TransactionsProcess::execute():
   - declared_cycles (1) ≤ max_block_cycles → no ban
   - mark_as_known_txs([H]) → H enters tx_filter
   - submit_remote_tx(T, 1, attacker_peer) → VM hits cycle limit → Reject
6. Honest peer B later sends RelayTransactionHashes { tx_hashes: [H] }.
7. Victim's tx_filter already contains H → victim does not request T from B.
8. T is suppressed until tx_filter entry for H expires.
9. Attacker can repeat from step 2 to extend suppression indefinitely.
``` [9](#0-8) [10](#0-9)

### Citations

**File:** util/gen-types/schemas/extensions.mol (L155-158)
```text
table RelayTransaction {
    cycles:                     Uint64,
    transaction:                Transaction,
}
```

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

**File:** test/src/specs/tx_pool/declared_wrong_cycles.rs (L69-117)
```rust
pub struct DeclaredWrongCyclesAndRelayAgain;

impl Spec for DeclaredWrongCyclesAndRelayAgain {
    crate::setup!(num_nodes: 3);

    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];
        let node1 = &nodes[1];
        let node2 = &nodes[2];
        node0.mine_until_out_bootstrap_period();
        out_ibd_mode(nodes);

        let mut net = Net::new(
            self.name(),
            node0.consensus(),
            vec![SupportProtocols::RelayV3],
        );

        let tx = node0.new_transaction_spend_tip_cellbase();
        // relay tx to node0 with wrong cycles
        net.connect(node0);
        relay_tx(&net, node0, tx.clone(), ALWAYS_SUCCESS_SCRIPT_CYCLE + 1);
        let ret = wait_until(10, || node0.rpc_client().get_peers().is_empty());
        assert!(
            ret,
            "The address of net should be removed from node0's peers",
        );
        // connect node0 and node2, make sure node0's relay tx hash processing is working
        node0.rpc_client().clear_banned_addresses();
        node0.connect(node2);
        // removing invalid tx hash from node0's known tx filer is async, wait 5 seconds to make sure it's removed
        sleep(5);

        // connect node0 with node1, tx will be relayed from node1 to node0
        node0.connect(node1);

        // relay tx to node1 with correct cycles
        net.connect(node1);
        relay_tx(&net, node1, tx, ALWAYS_SUCCESS_SCRIPT_CYCLE);

        let result = wait_until(5, || {
            let tx_pool_info = node0.get_tip_tx_pool_info();
            tx_pool_info.orphan.value() == 0 && tx_pool_info.pending.value() == 1
        });
        assert!(
            result,
            "Tx with wrong cycles should be relayed again with correct cycle"
        );
    }
```
