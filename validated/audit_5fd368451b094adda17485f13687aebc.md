### Title
CKB Transaction `since` Field Lacks Protocol-Level Upper-Bound (Deadline) — (`File: verification/src/transaction_verifier.rs`)

### Summary
CKB's `since` field (`SinceVerifier`) enforces only a "not-before" lower bound on transaction validity. There is no protocol-level "not-after" (deadline/expiry) mechanism. The only expiry that exists is the node-local `expiry_hours` config in the tx-pool, which is not a consensus rule and is not enforced on-chain. A miner can hold any valid transaction and commit it to the chain at any future time after the `since` condition is met, with no upper-bound constraint.

### Finding Description
The `SinceVerifier` in `verification/src/transaction_verifier.rs` implements RFC-0017 time locks. Both `verify_absolute_lock` and `verify_relative_lock` only check whether the current block number/epoch/timestamp is **less than** the required value, returning `Immature` if so. There is no corresponding "not-after" check anywhere in the verifier. [1](#0-0) 

The only expiry mechanism in the codebase is the tx-pool's `remove_expired`, which evicts transactions that have been in the local pool longer than `expiry_hours` (default: 12 hours). [2](#0-1) [3](#0-2) 

This eviction is:
1. **Node-local only** — not a consensus rule; different nodes can have different `expiry_hours` values.
2. **Not triggered periodically** — `remove_expired` is only called from `_update_tx_pool_for_reorg`, meaning it fires on chain reorganizations, not on a timer. [4](#0-3) 

A transaction evicted from one node's mempool can be resubmitted to another node and committed at any future time. There is no consensus-enforced deadline. [5](#0-4) [6](#0-5) 

### Impact Explanation
Any time-sensitive protocol built on CKB — atomic swaps, HTLCs, payment channels, or offer/bid scripts — is affected. A transaction sender cannot express "this transaction must be committed before block N or timestamp T" at the protocol level. A miner who receives such a transaction can:

1. Hold it in their local mempool (or re-inject it later).
2. Wait until conditions are maximally unfavorable for the sender (e.g., after an HTLC timeout, after a counterparty has reclaimed funds, or after an exchange rate has moved drastically).
3. Commit the transaction on-chain, forcing the sender into an outcome they did not intend.

Script authors who rely on `since` for time-sensitive logic have no way to add a corresponding upper-bound deadline at the consensus layer.

**Impact: Medium** — affects all time-sensitive CKB scripts/protocols; the sender has no protocol-level recourse once a transaction is broadcast.

### Likelihood Explanation
**Likelihood: Low** — requires a miner to actively hold and strategically time the submission of a transaction. However, no special privilege or majority hashpower is needed; any miner who receives the transaction can do this. The scenario becomes more likely as CKB-based DeFi/swap protocols grow.

### Recommendation
Add an optional `until` (not-after) field to the `since` semantics, or introduce a separate `deadline` field in `CellInput`, enforced in `SinceVerifier::verify()`. The verifier should reject a transaction if the current block number/epoch/timestamp **exceeds** the deadline value, analogous to how it currently rejects transactions that have not yet reached the `since` value.

Alternatively, document clearly that script authors must implement deadline logic themselves via header deps and script-level timestamp checks, and provide a standard library primitive for this pattern.

### Proof of Concept

1. Alice creates an atomic swap transaction with `since = 0` (no lower bound) and broadcasts it with a low fee.
2. The transaction sits in the mempool. After 12 hours, it is evicted from some nodes' pools (`remove_expired` fires on reorg).
3. A miner who retained the transaction (or to whom it is resubmitted) waits until Alice's swap counterparty has reclaimed their funds (after the HTLC timeout).
4. The miner now commits Alice's transaction on-chain. Alice's funds are spent, but the counterparty's side of the swap has already expired — Alice receives nothing.
5. There is no consensus rule that would have rejected the transaction at step 4, because `SinceVerifier` only checks `current >= since` (lower bound) and has no upper-bound check. [7](#0-6) [8](#0-7)

### Citations

**File:** verification/src/transaction_verifier.rs (L551-590)
```rust
/// The struct define wrapper for (unsigned 64-bit integer) tx field since
///
/// See [tx-since](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0017-tx-valid-since/0017-tx-valid-since.md)
#[derive(Copy, Clone, Debug)]
pub struct Since(pub u64);

impl Since {
    /// Whether since represented absolute form
    pub fn is_absolute(self) -> bool {
        self.0 & LOCK_TYPE_FLAG == 0
    }

    /// Whether since represented relative form
    #[inline]
    pub fn is_relative(self) -> bool {
        !self.is_absolute()
    }

    /// Whether since flag is valid
    pub fn flags_is_valid(self) -> bool {
        (self.0 & REMAIN_FLAGS_BITS == 0)
            && ((self.0 & METRIC_TYPE_FLAG_MASK) != METRIC_TYPE_FLAG_MASK)
    }

    /// Extracts a `SinceMetric` from an unsigned 64-bit integer since
    pub fn extract_metric(self) -> Option<SinceMetric> {
        let value = self.0 & VALUE_MASK;
        match self.0 & METRIC_TYPE_FLAG_MASK {
            //0b0000_0000
            0x0000_0000_0000_0000 => Some(SinceMetric::BlockNumber(value)),
            //0b0010_0000
            0x2000_0000_0000_0000 => Some(SinceMetric::EpochNumberWithFraction(
                EpochNumberWithFraction::from_full_value_unchecked(value),
            )),
            //0b0100_0000
            0x4000_0000_0000_0000 => value.checked_mul(1000).map(SinceMetric::Timestamp),
            _ => None,
        }
    }
}
```

**File:** verification/src/transaction_verifier.rs (L632-664)
```rust
    fn verify_absolute_lock(&self, index: usize, since: Since) -> Result<(), Error> {
        if since.is_absolute() {
            match since.extract_metric() {
                Some(SinceMetric::BlockNumber(block_number)) => {
                    let proposal_window = self.consensus.tx_proposal_window();
                    if self.tx_env.block_number(proposal_window) < block_number {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                Some(SinceMetric::EpochNumberWithFraction(epoch_number_with_fraction)) => {
                    if !epoch_number_with_fraction.is_well_formed_increment() {
                        return Err((TransactionError::InvalidSince { index }).into());
                    }
                    let a = self.tx_env.epoch().to_rational();
                    let b = epoch_number_with_fraction.normalize().to_rational();
                    if a < b {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                Some(SinceMetric::Timestamp(timestamp)) => {
                    let parent_hash = self.tx_env.parent_hash();
                    let tip_timestamp = self.block_median_time(&parent_hash);
                    if tip_timestamp < timestamp {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
                None => {
                    return Err((TransactionError::InvalidSince { index }).into());
                }
            }
        }
        Ok(())
    }
```

**File:** verification/src/transaction_verifier.rs (L735-759)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        for (index, (cell_meta, input)) in self
            .rtx
            .resolved_inputs
            .iter()
            .zip(self.rtx.transaction.inputs())
            .enumerate()
        {
            // ignore empty since
            let since: u64 = input.since().into();
            if since == 0 {
                continue;
            }
            let since = Since(since);
            // check remain flags
            if !since.flags_is_valid() {
                return Err((TransactionError::InvalidSince { index }).into());
            }

            // verify time lock
            self.verify_absolute_lock(index, since)?;
            self.verify_relative_lock(index, since, cell_meta)?;
        }
        Ok(())
    }
```

**File:** tx-pool/src/pool.rs (L55-57)
```rust
    pub fn new(config: TxPoolConfig, snapshot: Arc<Snapshot>) -> TxPool {
        let recent_reject = Self::build_recent_reject(&config);
        let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
```

**File:** tx-pool/src/pool.rs (L270-288)
```rust
    // Expire all transaction (and their dependencies) in the pool.
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L17-18)
```rust
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
```

**File:** tx-pool/src/process.rs (L1109-1110)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);
```

**File:** util/app-config/src/configs/tx_pool.rs (L41-42)
```rust
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
```
