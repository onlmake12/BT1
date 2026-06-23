### Title
Attacker-Controlled `cycles` Field in `RelayTransaction` Is Not Covered by Transaction Hash, Enabling Transaction Suppression via Deflated Cycle Declaration — (`sync/src/relayer/transactions_process.rs`, `tx-pool/src/process.rs`)

---

### Summary

The `RelayTransaction` P2P message carries a `cycles` field alongside the `transaction` body. This `cycles` value is provided by the relaying peer and is **not part of the transaction hash** (which covers only `RawTransaction` for `tx_hash`, or the full `Transaction` for `witness_hash`). The node uses the peer-supplied `declared_cycles` as the **hard cycle cap** for script execution. A malicious peer can send a valid transaction with a deliberately deflated `cycles` value, causing script execution to abort with `ExceededMaximumCycles` rather than `DeclaredWrongCycles`. Because the transaction hash is marked as "known" before pool submission, subsequent legitimate relays of the same transaction are silently dropped, achieving temporary transaction censorship without triggering the peer-ban path that `DeclaredWrongCycles` would normally invoke.

---

### Finding Description

**The unauthenticated `cycles` field is used as the execution cycle cap.**

The `RelayTransaction` molecule schema has two fields:

```
table RelayTransaction {
    cycles:      Uint64,
    transaction: Transaction,
}
``` [1](#0-0) 

The `cycles` field is entirely outside the transaction's cryptographic commitment. The transaction hash (`tx_hash`) covers only `RawTransaction`, and the witness hash covers the full `Transaction` struct — neither includes `cycles`. [2](#0-1) 

In `TransactionsProcess::execute`, the peer-supplied `cycles` is extracted and passed directly as `declared_cycles` to the tx-pool: [3](#0-2) 

The tx hash is then **marked as known before pool submission**: [4](#0-3) 

Inside `_process_tx`, `declared_cycles` is used as the **maximum cycle limit** for script execution: [5](#0-4) 

The `DeclaredWrongCycles` check only fires when execution **completes** and the result differs from the declared value: [6](#0-5) 

**The gap:** if `declared_cycles < actual_cycles`, execution is aborted by `verify_rtx` with `ExceededMaximumCycles` before the comparison is ever reached. The `DeclaredWrongCycles` path — which triggers peer banning — is never hit.

---

### Impact Explanation

A malicious peer executes the following steps:

1. Connects to a target CKB node.
2. Announces a valid transaction hash via `RelayTransactionHashes`.
3. Waits for the node to issue `GetRelayTransactions` for that hash (establishing the peer as the designated source).
4. Responds with a `RelayTransaction` carrying the correct `transaction` body but `cycles = 1` (or any value below the actual execution cost).

Consequences on the target node:
- The tx hash is added to `tx_filter` (marked known) **before** pool submission.
- Script execution is capped at 1 cycle and immediately fails with `ExceededMaximumCycles`.
- The rejection is **not** `DeclaredWrongCycles`, so the peer-ban path may not be triggered.
- All subsequent `RelayTransactions` messages from any peer carrying the same tx hash are silently discarded by the `tx_filter` check until the filter entry expires.

The result is **temporary transaction censorship**: a valid, fee-paying transaction is prevented from entering the pool for the duration of the filter TTL, without the attacker being banned.

The `DeclaredWrongCycles` test confirms that inflated cycles (where execution completes) correctly bans the peer: [7](#0-6) 

But deflated cycles (where execution is aborted early) bypass this protection entirely.

---

### Likelihood Explanation

Any unprivileged peer connected to a CKB node can execute this attack. The only prerequisite is that the attacker announces the target tx hash first and waits for the node to request it — a normal part of the relay protocol. The attack requires no special privileges, no keys, and no majority hashpower. It is reachable via the standard `RelayV3` P2P protocol. [8](#0-7) 

---

### Recommendation

When `verify_rtx` returns `ExceededMaximumCycles` and a `declared_cycles` value was provided by a remote peer, the node should re-execute the transaction with `max_block_cycles` as the cap. If the re-execution succeeds, the peer declared a falsely low cycle count and should be treated identically to `DeclaredWrongCycles` (peer ban, tx hash removed from the known filter). This closes the gap between the inflated-cycles and deflated-cycles code paths.

Alternatively, always execute with `max_block_cycles` as the cap and compare the result against `declared_cycles` afterward, eliminating the asymmetry entirely.

---

### Proof of Concept

1. Attacker peer connects to a target node via `RelayV3`.
2. Attacker sends `RelayTransactionHashes` with the hash of a valid, high-cycle transaction.
3. Node responds with `GetRelayTransactions` for that hash.
4. Attacker sends `RelayTransactions` with `cycles = 1` and the correct transaction body.
5. In `transactions_process.rs`, the tx hash is marked known (`mark_as_known_txs`) and `submit_remote_tx(tx, 1, peer)` is called.
6. In `_process_tx`, `max_cycles = 1`; `verify_rtx` aborts with `ExceededMaximumCycles`; the `DeclaredWrongCycles` branch is never reached; the peer is not banned.
7. The legitimate relay of the same transaction (from any peer) is now filtered out by `tx_filter` until expiry. [9](#0-8) [10](#0-9)

### Citations

**File:** util/gen-types/schemas/extensions.mol (L155-158)
```text
table RelayTransaction {
    cycles:                     Uint64,
    transaction:                Transaction,
}
```

**File:** util/gen-types/src/extension/calc_hash.rs (L125-152)
```rust
impl<'r> packed::RawTransactionReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the transaction hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_tx_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(RawTransaction, calc_tx_hash);

impl<'r> packed::TransactionReader<'r> {
    /// Calls [`RawTransactionReader.calc_tx_hash()`] for [`self.raw()`].
    ///
    /// [`RawTransactionReader.calc_tx_hash()`]: struct.RawTransactionReader.html#method.calc_tx_hash
    /// [`self.raw()`]: #method.raw
    pub fn calc_tx_hash(&self) -> packed::Byte32 {
        self.raw().calc_tx_hash()
    }

    /// Calculates the hash for [self.as_slice()] as the witness hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_witness_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(Transaction, calc_tx_hash);
impl_calc_special_hash_for_entity!(Transaction, calc_witness_hash);
```

**File:** sync/src/relayer/transactions_process.rs (L37-57)
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
```

**File:** sync/src/relayer/transactions_process.rs (L63-93)
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
