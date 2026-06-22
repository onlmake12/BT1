### Title
Unbounded Async Push in `handle_notify_new_block` Causes Indefinite Task Accumulation Under Block Flood - (File: `notify/src/lib.rs`)

### Summary
`handle_notify_new_block` in the `NotifyService` uses an unbounded `subscriber.send(block).await` with no timeout, unlike every other notification handler in the same file which uses `send_timeout`. A sync peer or block relayer that floods the node with valid blocks during IBD can cause the subscriber channel (capacity 128) to fill, causing an unbounded number of spawned tokio tasks to accumulate — each holding a live `BlockView` reference — leading to memory exhaustion and potential node unresponsiveness.

### Finding Description

In `notify/src/lib.rs`, `handle_notify_new_block` dispatches to each subscriber by spawning a task that calls `subscriber.send(block).await` with no timeout:

```rust
// notify/src/lib.rs lines 265-273
for subscriber in self.new_block_subscribers.values() {
    let block = block.clone();
    let subscriber = subscriber.clone();
    self.handle.spawn(async move {
        if let Err(e) = subscriber.send(block).await {   // ← NO timeout
            error!("Failed to notify new block, error: {}", e);
        }
    });
}
```

Every other notification handler in the same file uses `send_timeout`:

| Handler | Method |
|---|---|
| `handle_notify_new_transaction` | `subscriber.send_timeout(tx_entry, tx_timeout).await` |
| `handle_notify_proposed_transaction` | `subscriber.send_timeout(tx_entry, tx_timeout).await` |
| `handle_notify_reject_transaction` | `subscriber.send_timeout(tx_entry, tx_timeout).await` |
| `handle_notify_network_alert` | `subscriber.send_timeout(alert, alert_timeout).await` |
| **`handle_notify_new_block`** | **`subscriber.send(block).await` — no timeout** |

The `new_block_subscribers` channel is created with capacity `NOTIFY_CHANNEL_SIZE = 128`. Once the channel is full, each subsequent call to `handle_notify_new_block` spawns a new tokio task that suspends indefinitely on the `send` await point. These tasks are never cancelled and each holds a live `Arc<BlockView>` reference, preventing the block data from being freed.

### Impact Explanation

During IBD (Initial Block Download), the node processes blocks far faster than the normal 10-second epoch. A sync peer can deliver thousands of valid blocks in rapid succession. If the downstream subscriber (e.g., the RPC subscription service) cannot drain the channel at the same rate, the channel saturates at 128 entries. Every block beyond that spawns a new suspended task. With blocks up to the protocol's `max_block_bytes` limit, thousands of suspended tasks each holding a full `BlockView` can exhaust available memory, causing the node process to be killed by the OS OOM killer or become unresponsive.

**Impact: High** — node crash / unresponsiveness during IBD  
**Likelihood: Low** — requires a fast block-providing sync peer and a slow subscriber

### Likelihood Explanation

The attack is reachable by any unprivileged sync peer: the peer simply provides valid blocks at the maximum rate the sync protocol allows. No special privilege is required. The subscriber slowdown is a natural consequence of IBD block volume. The inconsistency with all other handlers (which have timeouts) suggests this was an oversight rather than intentional design.

### Recommendation

Apply the same `send_timeout` pattern used by all other notification handlers:

```rust
fn handle_notify_new_block(&self, block: BlockView) {
    let block_timeout = self.timeout.tx; // or a dedicated block timeout config
    for subscriber in self.new_block_subscribers.values() {
        let block = block.clone();
        let subscriber = subscriber.clone();
        self.handle.spawn(async move {
            if let Err(e) = subscriber.send_timeout(block, block_timeout).await {
                error!("Failed to notify new block, error: {}", e);
            }
        });
    }
    // ...
}
```

Add a `notify_block_timeout` field to `NotifyConfig` (in `util/app-config/src/configs/notify.rs`) analogous to the existing `notify_tx_timeout` and `notify_alert_timeout` fields.

### Proof of Concept

1. Connect to a CKB node as a sync peer.
2. During IBD, deliver valid blocks at the maximum rate the `SendBlock` sync message allows.
3. The RPC subscription service (or any registered `new_block_subscriber`) cannot drain the 128-slot channel at IBD speed.
4. After 128 blocks, every subsequent block causes `handle_notify_new_block` to spawn a new tokio task suspended on `subscriber.send(block).await`.
5. Each suspended task retains an `Arc<BlockView>`. Memory grows proportionally to `(num_blocks − 128) × avg_block_size`.
6. With sustained block delivery, the node's memory is exhausted and the process is terminated or becomes unresponsive.

**Root cause location:** [1](#0-0) 

**Inconsistency — all other handlers use `send_timeout`:** [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

**Channel capacity constant:** [6](#0-5) 

**Timeout config (missing block timeout):** [7](#0-6)

### Citations

**File:** notify/src/lib.rs (L62-66)
```rust
pub const SIGNAL_CHANNEL_SIZE: usize = 1;
/// Channel size for registration requests.
pub const REGISTER_CHANNEL_SIZE: usize = 2;
/// Channel size for notification messages.
pub const NOTIFY_CHANNEL_SIZE: usize = 128;
```

**File:** notify/src/lib.rs (L261-273)
```rust
    fn handle_notify_new_block(&self, block: BlockView) {
        trace!("New block event {:?}", block);
        let block_hash = block.hash();
        // notify all subscribers
        for subscriber in self.new_block_subscribers.values() {
            let block = block.clone();
            let subscriber = subscriber.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send(block).await {
                    error!("Failed to notify new block, error: {}", e);
                }
            });
        }
```

**File:** notify/src/lib.rs (L315-328)
```rust
    fn handle_notify_new_transaction(&self, tx_entry: PoolTransactionEntry) {
        trace!("New tx event {:?}", tx_entry);
        // notify all subscribers
        let tx_timeout = self.timeout.tx;
        // notify all subscribers
        for subscriber in self.new_transaction_subscribers.values() {
            let tx_entry = tx_entry.clone();
            let subscriber = subscriber.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send_timeout(tx_entry, tx_timeout).await {
                    error!("Failed to notify new transaction, error: {}", e);
                }
            });
        }
```

**File:** notify/src/lib.rs (L345-358)
```rust
    fn handle_notify_proposed_transaction(&self, tx_entry: PoolTransactionEntry) {
        trace!("Proposed tx event {:?}", tx_entry);
        // notify all subscribers
        let tx_timeout = self.timeout.tx;
        // notify all subscribers
        for subscriber in self.proposed_transaction_subscribers.values() {
            let tx_entry = tx_entry.clone();
            let subscriber = subscriber.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send_timeout(tx_entry, tx_timeout).await {
                    error!("Failed to notify proposed transaction, error {}", e);
                }
            });
        }
```

**File:** notify/src/lib.rs (L375-389)
```rust
    fn handle_notify_reject_transaction(&self, tx_entry: (PoolTransactionEntry, Reject)) {
        trace!("Tx reject event {:?}", tx_entry);
        // notify all subscribers
        let tx_timeout = self.timeout.tx;
        // notify all subscribers
        for subscriber in self.reject_transaction_subscribers.values() {
            let tx_entry = tx_entry.clone();
            let subscriber = subscriber.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send_timeout(tx_entry, tx_timeout).await {
                    error!("Failed to notify transaction reject, error: {}", e);
                }
            });
        }
    }
```

**File:** notify/src/lib.rs (L402-421)
```rust
    fn handle_notify_network_alert(&self, alert: Alert) {
        trace!("Network alert event {:?}", alert);
        let alert_timeout = self.timeout.alert;
        let message = alert
            .as_reader()
            .raw()
            .message()
            .as_utf8()
            .expect("alert message should be utf8")
            .to_owned();
        // notify all subscribers
        for subscriber in self.network_alert_subscribers.values() {
            let subscriber = subscriber.clone();
            let alert = alert.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send_timeout(alert, alert_timeout).await {
                    error!("Failed to notify network_alert, error: {}", e);
                }
            });
        }
```

**File:** util/app-config/src/configs/notify.rs (L1-26)
```rust
use serde::{Deserialize, Serialize};
/// Notify config options.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Default, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// An executable script to be called whenever there's a new block in the canonical chain.
    ///
    /// The script is called with the block hash as the argument.
    pub new_block_notify_script: Option<String>,
    /// An executable script to be called whenever there's a new network alert received.
    ///
    /// The script is called with the alert message as the argument.
    pub network_alert_notify_script: Option<String>,

    /// Notify tx timeout in milliseconds
    #[serde(default, deserialize_with = "at_least_100")]
    pub notify_tx_timeout: Option<u64>,

    /// Notify alert timeout in milliseconds
    #[serde(default, deserialize_with = "at_least_100")]
    pub notify_alert_timeout: Option<u64>,

    /// Notify alert timeout in milliseconds
    #[serde(default, deserialize_with = "at_least_100")]
    pub script_timeout: Option<u64>,
}
```
