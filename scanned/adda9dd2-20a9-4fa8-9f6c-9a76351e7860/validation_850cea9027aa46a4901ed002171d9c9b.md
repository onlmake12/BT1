Looking at the M-08 vulnerability class: **griefing via mutable-state manipulation to block a legitimate operation**. The pattern is: a function checks a mutable value as a precondition; an attacker manipulates that value before the check runs, causing the operation to fail.

I need to find this pattern in CKB's production code — specifically a check on mutable pool/chain state that an unprivileged peer can manipulate to block a legitimate operation.

The most direct analog is in the RBF (Replace-by-Fee) path in `tx-pool/src/pool.rs`.

---

### Title
Attacker Can Permanently Block RBF Transaction Replacement by Front-Running with Descendant Transactions — (`tx-pool/src/pool.rs`)

### Summary

The `check_rbf` function enforces `MAX_REPLACEMENT_CANDIDATES = 100`: if the transaction being replaced has more than 100 descendants in the pool, the replacement is rejected. An attacker who receives even one output from the victim's in-pool transaction can front-run the victim's RBF attempt by submitting a chain of 100 cheap descendant transactions, causing the replacement to fail indefinitely with `RBFRejected("Tx conflict with too many txs")`. This is a direct analog to M-08: mutable pool state (descendant count) is manipulated by an unprivileged actor to block a legitimate operation (fee-bump via RBF).

### Finding Description

In `tx-pool/src/pool.rs`, the constant and the check are:

```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
``` [1](#0-0) 

```rust
pub(crate) fn check_rbf(
    &self,
    snapshot: &Snapshot,
    entry: &TxEntry,
) -> Result<HashSet<ProposalShortId>, Reject> {
``` [2](#0-1) 

Rule #5 of the RBF check counts all descendants of every conflicted transaction and rejects if the total exceeds 100:

```rust
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;
    if replace_count > MAX_REPLACEMENT_CANDIDATES {
        return Err(Reject::RBFRejected(format!(
            "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
            replace_count, MAX_REPLACEMENT_CANDIDATES,
        )));
    }
``` [3](#0-2) 

**Attack flow:**

1. Alice submits `tx_alice` spending cell A, producing cell B (Alice's change) and cell C (Bob's payment output).
2. Bob (attacker) creates a chain of 100 transactions: `tx_b1` spends cell C, `tx_b2` spends `tx_b1`'s output, …, `tx_b100` spends `tx_b99`'s output. Each pays only the minimum fee rate.
3. Bob submits all 100 to the pool via `send_transaction` RPC — all are valid because Bob owns cell C.
4. Alice's original `tx_alice` now has 100 descendants in the pool.
5. Alice submits `tx_alice2` (same input cell A, higher fee) to bump her stuck transaction.
6. `check_rbf` computes `replace_count = 100 + 1 = 101 > MAX_REPLACEMENT_CANDIDATES`.
7. Alice's replacement is rejected: `RBFRejected("Tx conflict with too many txs, conflict txs count: 101, expect <= 100")`.

The `send_transaction` RPC is the unprivileged entry point: [4](#0-3) 

The rejection propagates through `from_submit_transaction_reject`: [5](#0-4) 

Bob can resubmit his descendant chain whenever it is evicted by the pool's size-limit eviction (`limit_size`), sustaining the attack indefinitely at minimal cost: [6](#0-5) 

### Impact Explanation

- Alice cannot fee-bump her stuck low-fee transaction via RBF for as long as Bob maintains 100 descendants in the pool.
- During network congestion (high-fee periods), Alice's transaction may remain unconfirmed indefinitely.
- The attacker sustains the attack by resubmitting descendants after eviction; the cost is 100 × `min_fee_rate` per eviction cycle — negligible.
- No theft occurs, but the victim's funds are effectively frozen in an unconfirmable in-pool transaction, matching the M-08 "suboptimal/missed opportunity" impact class.

### Likelihood Explanation

- The attacker only needs to be a **payment recipient** of the victim's transaction — a completely normal, unprivileged role.
- No special keys, admin access, or majority hashpower is required.
- The attack is cheap: 100 minimum-fee transactions. On CKB mainnet with `min_fee_rate = 1000 shannons/KB`, each transaction costs on the order of hundreds of shannons.
- The attack is repeatable: whenever Bob's descendants are evicted, he resubmits them before Alice can get her replacement accepted.
- RBF is explicitly enabled in the default CKB configuration (`min_rbf_rate = 1500 > min_fee_rate = 1000`), making this code path active on mainnet. [7](#0-6) 

### Recommendation

- **Short-term**: When an RBF replacement is submitted by the owner of the conflicted transaction's inputs, allow eviction of low-fee descendants that were added *after* the original transaction, rather than hard-rejecting the replacement.
- **Alternative**: Reduce `MAX_REPLACEMENT_CANDIDATES` is not the fix — the real fix is to not count descendants whose *only* connection to the conflict set is through outputs the original sender did not control.
- **Mitigation analog to M-08**: Add a mechanism (e.g., a signed "pause descendants" flag or a priority-RBF path) that lets the original transaction sender bypass the descendant count limit when replacing their own transaction.

### Proof of Concept

```
# Setup: RBF enabled (min_rbf_rate > min_fee_rate in ckb.toml)

1. Alice: send_transaction(tx_alice)
   - input:  cell_A  (owned by Alice)
   - output0: cell_B (owned by Alice, change)
   - output1: cell_C (owned by Bob, payment)

2. Bob (attacker): for i in 1..=100:
       send_transaction(tx_b_i)
       - input:  output of tx_b_{i-1} (or cell_C for i=1)
       - output: new cell owned by Bob
       - fee:    min_fee_rate

3. Alice: send_transaction(tx_alice2)
   - input:  cell_A  (same as tx_alice)
   - output: cell_B' (higher fee, less change)

4. Result: RPC returns error -1111 (PoolRejectedRBF):
   "RBFRejected: Tx conflict with too many txs,
    conflict txs count: 101, expect <= 100"

# Alice is permanently blocked from fee-bumping tx_alice
# as long as Bob resubmits his 100-tx chain after each eviction.
```

The root cause is in `check_rbf` at the descendant count check: [3](#0-2) 

which reads mutable pool state (`calc_descendants`) that any unprivileged transaction sender can inflate by spending outputs they legitimately received.

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L80-83)
```rust
    /// Check whether tx-pool enable RBF
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```

**File:** tx-pool/src/pool.rs (L292-328)
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
```

**File:** tx-pool/src/pool.rs (L574-578)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
```

**File:** tx-pool/src/pool.rs (L611-624)
```rust
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
```

**File:** rpc/src/module/pool.rs (L106-111)
```rust
    #[rpc(name = "send_transaction")]
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256>;
```

**File:** rpc/src/error.rs (L191-191)
```rust
            Reject::RBFRejected(_) => RPCError::PoolRejectedRBF,
```
