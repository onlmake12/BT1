### Title
Tx-Pool Wall-Clock Expiry Accumulates During IBD, Causing Premature Transaction Rejection — (File: `tx-pool/src/pool.rs`)

### Summary
`TxPool::remove_expired()` expires pending transactions using raw wall-clock time. During Initial Block Download (IBD), the node cannot include new transactions in blocks, yet the expiry countdown runs uninterrupted. A transaction submitted at the start of a long IBD session will be silently dropped before the node ever has a chance to mine it, forcing the user to resubmit with no indication of what happened.

### Finding Description

**Root cause — wall-clock expiry with no IBD awareness**

`TxEntry::timestamp` is stamped with `unix_time_as_millis()` at submission time:

```rust
// tx-pool/src/component/entry.rs:48-49
pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
    Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
}
```

`TxPool::expiry` is a fixed millisecond duration derived from `expiry_hours` (default **12 h**):

```rust
// tx-pool/src/pool.rs:57
let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
```

`remove_expired()` fires on every block commit (called from `_update_tx_pool_for_reorg`) and checks only wall-clock elapsed time:

```rust
// tx-pool/src/pool.rs:271-288
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let removed: Vec<_> = self
        .pool_map
        .iter()
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        ...
```

There is no check for `is_initial_block_download()` before expiring entries. During IBD the node commits historical blocks at high speed, so `remove_expired` is invoked very frequently, but the wall-clock keeps advancing regardless.

**The paused-state analogy**

During IBD the node is in a functionally "paused" state for new-transaction inclusion: it is replaying historical blocks and will not mine any new block. A user who submits a transaction at IBD start:
- cannot get it included (the node is not producing new blocks),
- cannot meaningfully "refresh" it (the pool accepts it but it will never be proposed),
- watches the wall-clock expiry window drain away silently.

After `expiry_hours` of real time the entry is evicted with `Reject::Expiry(timestamp)` and the user must resubmit — with no protocol-level signal that IBD was the cause.

The same pattern applies to the `OrphanPool`, where `expires_at` is also set from `unix_time()` at insertion with no IBD guard:

```rust
// tx-pool/src/component/orphan.rs:36
expires_at: ckb_systemtime::unix_time().as_secs() + ORPHAN_TX_EXPIRE_TIME,
```

### Impact Explanation
A transaction submitted to a node that is in IBD for longer than `expiry_hours` (default 12 h, configurable) will be permanently dropped from the pool. The submitter receives a `Reject::Expiry` callback but no indication that IBD was the cause. The user must detect the situation out-of-band and resubmit, potentially paying a higher fee rate if network conditions changed. For time-sensitive transactions (e.g., time-locked cells, DAO withdrawals with deadline constraints) this silent expiry can cause missed windows.

### Likelihood Explanation
A fresh CKB full node syncing from genesis takes well over 12 hours on typical hardware. Any transaction submitted to such a node — by an RPC caller, a local wallet, or a relay peer — will expire before IBD completes. The default `expiry_hours = 12` is documented and widely used; operators who do not raise it are silently affected.

### Recommendation
Before expiring an entry in `remove_expired()`, check whether the node is in IBD and skip expiry (or pause the countdown) until IBD exits. Alternatively, reset `entry.timestamp` to `unix_time_as_millis()` when the node transitions out of IBD, so the 12-hour window starts only when the node can actually mine. The same fix should be applied to `OrphanPool::limit_size()`.

### Proof of Concept
1. Start a fresh CKB node with default config (`expiry_hours = 12`). The node enters IBD.
2. Submit any valid transaction via `send_transaction` RPC. The entry is stored with `timestamp = T_submit`.
3. Allow IBD to run for more than 12 hours of wall-clock time (normal for a full sync from genesis).
4. Observe that `remove_expired` (called on every block commit during IBD) evaluates `expiry + T_submit < now_ms` → `true` and emits `Reject::Expiry(T_submit)`.
5. After IBD completes, query the pool: the transaction is gone. The user must resubmit.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) 
<cite repo="Rodmore11/ckb--013" path="tx-pool/src/component/orphan.rs" start="

### Citations

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

**File:** tx-pool/src/component/entry.rs (L46-50)
```rust
impl TxEntry {
    /// Create new transaction pool entry
    pub fn new(rtx: Arc<ResolvedTransaction>, cycles: Cycle, fee: Capacity, size: usize) -> Self {
        Self::new_with_timestamp(rtx, cycles, fee, size, unix_time_as_millis())
    }
```
