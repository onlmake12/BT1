Audit Report

## Title
Missing `send_timeout` in `handle_notify_new_block` Allows Unbounded Task Accumulation Under Block Flood - (File: `notify/src/lib.rs`)

## Summary
`handle_notify_new_block` in `NotifyService` dispatches to subscribers via `subscriber.send(block).await` with no timeout, unlike every other notification handler in the same file which uses `send_timeout`. When a subscriber's channel (capacity 128) is saturated — a realistic condition during IBD — each subsequent block causes a new tokio task to be spawned and suspended indefinitely, each holding a live `Arc<BlockView>`. Sustained block delivery can exhaust node memory and cause a crash or OOM kill.

## Finding Description
The code at `notify/src/lib.rs` lines 261–273 confirms the inconsistency exactly as claimed:

```rust
fn handle_notify_new_block(&self, block: BlockView) {
    for subscriber in self.new_block_subscribers.values() {
        let block = block.clone();
        let subscriber = subscriber.clone();
        self.handle.spawn(async move {
            if let Err(e) = subscriber.send(block).await {   // ← no timeout
                error!("Failed to notify new block, error: {}", e);
            }
        });
    }
    // ...
}
```

All other handlers (`handle_notify_new_transaction`, `handle_notify_proposed_transaction`, `handle_notify_reject_transaction`, `handle_notify_network_alert`) use `subscriber.send_timeout(…, timeout).await`. The `NotifyTimeout` struct (lines 76–80) has `tx`, `alert`, and `script` fields but no block field, and `NotifyConfig` (util/app-config/src/configs/notify.rs lines 1–26) has no `notify_block_timeout`.

The event loop at lines 196–219 is non-blocking with respect to the spawned tasks: it calls `handle_notify_new_block`, which spawns tasks and returns immediately, then processes the next message. The upstream `notify_new_block` (lines 483–490) is also fire-and-forget — it spawns a task sending to `new_block_notifier` (cap 128) and returns to the caller without blocking. This means the chain service is never back-pressured by a slow subscriber. Once the 128-slot subscriber channel fills, every additional block spawns a new suspended task holding a distinct `Arc<BlockView>` reference, preventing GC of that block's data.

Existing guards are insufficient: the 128-slot channel provides a fixed buffer but no relief valve. Without a timeout, tasks never self-cancel.

## Impact Explanation
A node running with at least one registered `new_block_subscriber` (e.g., the RPC WebSocket subscription service) and processing blocks faster than the subscriber can drain them will accumulate suspended tokio tasks, each retaining a full `BlockView`. With CKB's `max_block_bytes` limit, thousands of such tasks can exhaust available RAM, causing the OS OOM killer to terminate the node process. This maps to **High: Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The preconditions are: (1) at least one active `new_block_subscriber` (the RPC subscription service registers one whenever any WebSocket client subscribes to new blocks), and (2) block delivery rate exceeds subscriber drain rate. During IBD, a fast sync peer can deliver valid blocks at the maximum rate the sync protocol allows. Block validation is CPU-bound and can be faster than a slow or overloaded RPC subscriber. No special privilege is required — any unprivileged sync peer can provide valid blocks at maximum rate. The inconsistency with all other handlers strongly suggests an oversight rather than intentional design. Likelihood is **Low** but non-negligible for nodes with active RPC subscribers during IBD.

## Recommendation
Apply the same `send_timeout` pattern used by all other handlers. Add a `block` field to `NotifyTimeout` (notify/src/lib.rs lines 76–80) and a corresponding `notify_block_timeout` field to `NotifyConfig` (util/app-config/src/configs/notify.rs), then change the dispatch loop:

```rust
fn handle_notify_new_block(&self, block: BlockView) {
    let block_timeout = self.timeout.block; // new field
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

## Proof of Concept
1. Start a CKB node with at least one WebSocket client subscribed to `new_block` (this registers a `new_block_subscriber` with a 128-slot channel).
2. During IBD, connect a sync peer that delivers valid blocks at the maximum rate the `SendBlock` sync message allows.
3. Ensure the WebSocket subscriber is slow to drain (e.g., simulate a slow client or many clients).
4. After 128 blocks, the subscriber channel is full. Each subsequent block causes `handle_notify_new_block` to spawn a new task suspended on `subscriber.send(block).await`.
5. Monitor tokio task count and RSS memory: both grow proportionally to `(blocks_processed − 128) × avg_block_size`.
6. With sustained delivery, RSS grows until the OOM killer terminates the process.

A unit test can reproduce this by: creating a `NotifyService` with one subscriber, never reading from the subscriber's receiver, sending >128 blocks via `notify_new_block`, and asserting that spawned task count grows unboundedly (e.g., via `tokio::runtime::Handle::metrics().num_alive_tasks()`).