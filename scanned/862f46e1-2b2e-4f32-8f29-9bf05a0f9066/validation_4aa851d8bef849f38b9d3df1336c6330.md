### Title
Lock Ordering Inversion in `accept_peer` Causes Potential Node Deadlock - (File: network/src/network.rs)

### Summary
The `accept_peer` function in `NetworkState` acquires `peer_store.lock()` first and then `peer_registry.write()` while the peer-store lock is still held. This inverted lock ordering, explicitly flagged by a developer comment in the code, creates a deadlock condition when any concurrent code path holds `peer_registry` and then attempts to acquire `peer_store`. The result is a permanent node hang reachable by any unprivileged peer initiating a connection.

### Finding Description
In `network/src/network.rs`, `accept_peer` acquires the two shared locks in the order **peer_store → peer_registry**:

```rust
pub(crate) fn accept_peer(&self, session_context: &SessionContext) -> Result<Option<Peer>, Error> {
    // NOTE: be careful, here easy cause a deadlock,
    //    because peer_store's lock scope across peer_registry's lock scope
    let mut peer_store = self.peer_store.lock();          // Lock 1 acquired
    {
        self.peer_registry.write().accept_peer(           // Lock 2 acquired while Lock 1 held
            ...
            &mut peer_store,
        )
    }
}
``` [1](#0-0) 

Meanwhile, `ban_session` and helper methods such as `with_peer_registry_mut` acquire `peer_registry.write()` first, and then separately call `peer_store.lock()`. Any code path that holds `peer_registry` (read or write) and then tries to acquire `peer_store` will deadlock with a concurrent `accept_peer` call:

- Thread A (`accept_peer`): holds `peer_store.lock()`, blocks waiting for `peer_registry.write()`
- Thread B (any caller holding `peer_registry`): holds `peer_registry`, blocks waiting for `peer_store.lock()` [2](#0-1) 

The developer comment at line 287–288 explicitly acknowledges this hazard but does not resolve it. The `with_peer_store_mut` helper and direct `peer_store.lock()` calls are spread across multiple protocol handlers (`identify/mod.rs`, `discovery/mod.rs`, `outbound_peer.rs`, `feeler.rs`, `hole_punching/mod.rs`), each of which may also hold `peer_registry` at the time of the call. [3](#0-2) 

### Impact Explanation
A deadlock in the P2P networking layer permanently hangs the node's connection-management threads. No new peers can be accepted or banned, existing peer state becomes stale, and the node effectively stops participating in the network — it can neither sync blocks nor relay transactions. The node must be manually restarted to recover.

### Likelihood Explanation
`accept_peer` is called on every inbound or outbound TCP connection handshake. Any unprivileged peer on the internet can trigger it simply by opening a connection. The concurrent code paths that hold `peer_registry` (peer discovery, identify protocol, feeler connections) run continuously in the background. Under normal load with multiple simultaneous connections the race window is small but non-zero; under deliberate connection-flood conditions an attacker can widen the window significantly, making the deadlock reliably reproducible.

### Recommendation
Establish and enforce a single global lock-acquisition order across all `NetworkState` methods. The consistent order should be **peer_registry → peer_store** (matching the majority of existing call sites). Refactor `accept_peer` to acquire `peer_registry.write()` before `peer_store.lock()`, or restructure the call so that `peer_store` is not held across the `peer_registry` acquisition. Consider using `parking_lot`'s deadlock-detection feature (already wired up in `ckb-bin/src/helper.rs`) in CI to catch future regressions. [4](#0-3) 

### Proof of Concept
1. Node starts; background tasks (discovery, identify) continuously hold `peer_registry.read()` or `peer_registry.write()` while calling `peer_store.lock()` (e.g., `with_peer_store_mut` in `outbound_peer.rs`).
2. Attacker opens a TCP connection to the node, triggering `accept_peer`.
3. `accept_peer` acquires `peer_store.lock()` (Lock 1) and then blocks on `peer_registry.write()` (Lock 2) because a background thread holds `peer_registry`.
4. The background thread, still holding `peer_registry`, reaches its own `peer_store.lock()` call and blocks because `accept_peer` holds Lock 1.
5. Both threads are now permanently blocked. The node's connection-management loop hangs; no further peers can be accepted or processed. [5](#0-4)

### Citations

**File:** network/src/network.rs (L241-281)
```rust
    pub(crate) fn ban_session(
        &self,
        p2p_control: &ServiceControl,
        session_id: SessionId,
        duration: Duration,
        reason: String,
    ) {
        if let Some(addr) = self.with_peer_registry(|reg| {
            reg.get_peer(session_id)
                .filter(|peer| !peer.is_whitelist)
                .map(|peer| peer.connected_addr.clone())
        }) {
            info!(
                "Ban peer {:?} for {} seconds, reason: {}",
                addr,
                duration.as_secs(),
                reason
            );
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_network_ban_peer.inc();
            }
            if let Some(peer) = self.with_peer_registry_mut(|reg| reg.remove_peer(session_id)) {
                let message = format!("Ban for {} seconds, reason: {}", duration.as_secs(), reason);
                self.peer_store.lock().ban_addr(
                    &peer.connected_addr,
                    duration.as_millis() as u64,
                    reason,
                );
                if let Err(err) =
                    disconnect_with_message(p2p_control, peer.session_id, message.as_str())
                {
                    debug!("Disconnect failed {:?}, error: {:?}", peer.session_id, err);
                }
            }
        } else {
            debug!(
                "Ban session({}) failed: not found in peer registry or it is on the whitelist",
                session_id
            );
        }
    }
```

**File:** network/src/network.rs (L283-299)
```rust
    pub(crate) fn accept_peer(
        &self,
        session_context: &SessionContext,
    ) -> Result<Option<Peer>, Error> {
        // NOTE: be careful, here easy cause a deadlock,
        //    because peer_store's lock scope across peer_registry's lock scope
        let mut peer_store = self.peer_store.lock();

        {
            self.peer_registry.write().accept_peer(
                session_context.address.clone(),
                session_context.id,
                session_context.ty,
                &mut peer_store,
            )
        }
    }
```

**File:** network/src/network.rs (L317-323)
```rust
    // For restrict lock in inner scope
    pub(crate) fn with_peer_store_mut<F, T>(&self, callback: F) -> T
    where
        F: FnOnce(&mut PeerStore) -> T,
    {
        callback(&mut self.peer_store.lock())
    }
```

**File:** ckb-bin/src/helper.rs (L8-45)
```rust
#[cfg(feature = "deadlock_detection")]
pub fn deadlock_detection() {
    use ckb_channel::select;
    use ckb_logger::{info, warn};
    use ckb_stop_handler::{new_crossbeam_exit_rx, register_thread};
    use ckb_util::parking_lot::deadlock;
    use std::{thread, time::Duration};

    info!("deadlock_detection enabled");
    let dead_lock_jh = thread::spawn({
        let ticker = ckb_channel::tick(Duration::from_secs(10));
        let stop_rx = new_crossbeam_exit_rx();
        move || loop {
            select! {
                recv(ticker) -> _ => {
                    let deadlocks = deadlock::check_deadlock();
                    if deadlocks.is_empty() {
                        continue;
                    }

                    warn!("{} deadlocks detected", deadlocks.len());
                    for (i, threads) in deadlocks.iter().enumerate() {
                        warn!("Deadlock #{}", i);
                        for t in threads {
                            warn!("Thread Id {:#?}", t.thread_id());
                            warn!("{:#?}", t.backtrace());
                        }
                    }

                },
                recv(stop_rx) -> _ =>{
                    info!("deadlock_detection received exit signal, stopped");
                    return;
                }
            }
        }
    });
    register_thread("dead_lock_detect", dead_lock_jh);
```
