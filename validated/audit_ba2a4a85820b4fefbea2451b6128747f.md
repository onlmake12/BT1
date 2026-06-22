### Title
Structural Front-Running via RBF Replacement of Proposed Transactions During Two-Phase Commit Window - (File: tx-pool/src/pool.rs)

---

### Summary

CKB's two-phase commit mechanism mandates that every non-cellbase transaction be publicly proposed on-chain for 2–10 blocks before it can be committed. When RBF is enabled (`min_rbf_rate > min_fee_rate`), the `check_rbf` function in `tx-pool/src/pool.rs` does not check whether the conflicting transaction is in `Pending`, `Gap`, or `Proposed` state. This means any unprivileged tx-pool submitter can observe a proposed transaction via `get_raw_tx_pool` or on-chain block data, then submit a competing higher-fee transaction to evict it. The original transaction is permanently marked `Rejected` and the attacker's transaction is committed instead.

---

### Finding Description

CKB's consensus protocol enforces a two-step transaction confirmation model defined by `ProposalWindow(2, 10)` in `spec/src/consensus.rs`. A transaction proposed at block height `h_p` can only be committed between heights `h_p + 2` and `h_p + 10`. During this mandatory window the full transaction — including its inputs, outputs, and fee — is permanently recorded in the on-chain proposal zone and simultaneously visible in the node's tx-pool.

The `get_raw_tx_pool` RPC (`rpc/src/module/pool.rs`) returns all `pending` and `proposed` transactions with their fees and sizes to any caller without authentication. This gives any observer a complete, real-time view of every transaction awaiting commitment.

When RBF is enabled, `check_rbf` in `tx-pool/src/pool.rs` evaluates only four rules:

- Rule #1 – RBF is enabled on the node.
- Rule #2 – The replacement does not introduce new unconfirmed inputs.
- Rule #5 – The replaced transaction has ≤ 100 descendants.
- Rules #3/#4 – The replacement fee exceeds the sum of replaced fees plus `min_rbf_rate × size`.

**There is no rule that prevents replacing a transaction already in `Proposed` state.** The comment in `test/src/specs/tx_pool/replace.rs` at line 754 explicitly states: *"We removed rule #6, this spec testing that we can replace tx in `Gap` and `Proposed` successfully."* The test `RbfReplaceProposedSuccess` confirms the attack end-to-end: a transaction in `Proposed` state is successfully evicted by a higher-fee replacement submitted via `send_transaction`.

The attack sequence is:

1. Attacker calls `get_raw_tx_pool(verbose=true)` and identifies a proposed transaction spending a publicly-accessible cell (e.g., a DEX order cell whose lock script permits any filler, a TYPE_ID cell, or any script that allows third-party interaction).
2. Attacker constructs a competing transaction spending the same input cell(s) with a fee exceeding `sum(replaced_fees) + min_rbf_rate × size`.
3. Attacker submits the competing transaction via `send_transaction`.
4. `check_rbf` passes; `process_rbf` removes the original from the pool and marks it `Rejected`.
5. The attacker's transaction enters `Pending`, gets proposed, and is committed. The original sender's transaction is permanently lost.

The proposal window amplifies the attack surface: even a transaction submitted privately to a single node will be broadcast on-chain in the proposal zone within one block, giving every observer a 2–10 block window to react.

---

### Impact Explanation

Any transaction that interacts with a cell whose lock or type script permits third-party spending (DEX order cells, open-auction cells, DAO withdrawal cells with predictable arguments, TYPE_ID-governed cells) is front-runnable during the entire proposal window. The attacker captures the economic opportunity (order fill, auction win, DAO reward) while the original submitter pays the cost of a failed transaction. Because the attacker only needs to outbid the original fee by `min_rbf_rate × size` (a small fixed increment), the attack is almost always profitable when the underlying economic opportunity exceeds that threshold — directly mirroring the `matchOrders()` dynamic described in the reference report.

---

### Likelihood Explanation

RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a node-operator configuration choice. However, as DeFi activity on CKB grows, operators running relayer or DEX infrastructure have strong incentive to enable RBF for fee-market efficiency. Once enabled, the attack requires only: (a) an RPC connection to any CKB node (unauthenticated by default), (b) the ability to construct a valid competing transaction, and (c) a fee increment above the RBF threshold. No privileged access, key material, or majority hashpower is needed.

---

### Recommendation

1. **Re-introduce a status guard in `check_rbf`**: Reject RBF replacement of any transaction whose pool status is `Proposed` or `Gap`. A transaction already committed to the on-chain proposal zone has passed the first confirmation step; evicting it resets the clock and creates the front-running window.

2. **Restrict `get_raw_tx_pool` verbose output**: Consider gating the fee/size details behind an authenticated or local-only RPC flag, reducing the information available to remote observers.

3. **Document the front-running surface**: Advise application developers building DEX or open-auction protocols on CKB to use lock scripts that bind the filler address (analogous to the "matching relayer" model in the reference report), so that only a designated address can submit the competing transaction.

---

### Proof of Concept

The existing integration test `RbfReplaceProposedSuccess` in `test/src/specs/tx_pool/replace.rs` is a complete proof of concept: [1](#0-0) 

The test mines until a transaction reaches `Status::Proposed` (on-chain in the proposal zone), then calls `send_transaction` with a higher-fee replacement and asserts `res.is_ok()` — confirming the proposed transaction is evicted and the replacement accepted. [2](#0-1) 

The root cause is the absence of a status check in `check_rbf`: [3](#0-2) 

No branch in `check_rbf` inspects `entry.status` or the status of the conflicting pool entry. All four implemented rules are purely fee- and graph-topology-based. [4](#0-3) 

The mandatory public disclosure window that creates the observation opportunity is defined here: [5](#0-4) 

And the unauthenticated RPC that exposes all proposed transactions with fees is: [6](#0-5) [7](#0-6)

### Citations

**File:** test/src/specs/tx_pool/replace.rs (L752-755)
```rust
pub struct RbfReplaceProposedSuccess;

// RBF Rule #6
// We removed rule #6, this spec testing that we can replace tx in `Gap` and `Proposed` successfully.
```

**File:** test/src/specs/tx_pool/replace.rs (L814-825)
```rust
        // begin to RBF
        let res = node0
            .rpc_client()
            .send_transaction_result(tx2.data().into());
        assert!(res.is_ok());

        let old_tx_status = node0.rpc_client().get_transaction(tx1_hash).tx_status;
        assert_eq!(old_tx_status.status, Status::Rejected);
        assert!(old_tx_status.reason.unwrap().contains("RBFRejected"));

        let tx2_status = node0.rpc_client().get_transaction(tx2.hash()).tx_status;
        assert_eq!(tx2_status.status, Status::Pending);
```

**File:** tx-pool/src/pool.rs (L574-585)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }
```

**File:** tx-pool/src/pool.rs (L589-678)
```rust
        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }

            if !descendants.is_disjoint(&ancestors) {
                return Err(Reject::RBFRejected(
                    "Tx ancestors have common with conflict Tx descendants".to_string(),
                ));
            }

            let entries = descendants
                .iter()
                .filter_map(|id| self.get_pool_entry(id))
                .collect::<Vec<_>>();

            for entry in entries.iter() {
                let hash = entry.inner.transaction().hash();
                if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
                    return Err(Reject::RBFRejected(
                        "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
                    ));
                }
            }
            all_conflicted.extend(entries);
        }

        let tx_cells_deps: Vec<OutPoint> = entry
            .transaction()
            .cell_deps_iter()
            .map(|c| c.out_point())
            .collect();
        for entry in all_conflicted.iter() {
            let hash = entry.inner.transaction().hash();
            if tx_cells_deps.iter().any(|pt| pt.tx_hash() == hash) {
                return Err(Reject::RBFRejected(
                    "new Tx contains cell deps from conflicts".to_string(),
                ));
            }
        }

        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }

        Ok(conflict_ids)
```

**File:** spec/src/consensus.rs (L107-137)
```rust
/// The struct represent CKB two-step-transaction-confirmation params
///
/// [two-step-transaction-confirmation params](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0020-ckb-consensus-protocol/0020-ckb-consensus-protocol.md#two-step-transaction-confirmation)
///
/// The `ProposalWindow` consists of two fields:
/// self.0, aka w_close, self.1, aka w_far
/// w_close and w_far define the closest and farthest on-chain distance between a transaction’s proposal and commitment.
#[derive(Clone, PartialEq, Debug, Eq, Copy)]
pub struct ProposalWindow(pub BlockNumber, pub BlockNumber);

/// "TYPE_ID" in hex
pub const TYPE_ID_CODE_HASH: H256 = h256!("0x545950455f4944");

/// Two protocol parameters w_close and w_far define the closest
/// and farthest on-chain distance between a transaction's proposal
/// and commitment.
///
/// A non-cellbase transaction is committed at height h_c if all of the following conditions are met:
/// 1) it is proposed at height h_p of the same chain, where w_close <= h_c − h_p <= w_far ;
/// 2) it is in the commitment zone of the main chain block with height h_c ;
///
/// ```text
/// ProposalWindow (2, 10)
///     propose
///        \
///         \
///         13 14 [15 16 17 18 19 20 21 22 23]
///                \_______________________/
///                             \
///                           commit
/// ```
```

**File:** rpc/src/module/pool.rs (L394-395)
```rust
    #[rpc(name = "get_raw_tx_pool")]
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool>;
```

**File:** rpc/src/module/pool.rs (L703-718)
```rust
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
    }
```
