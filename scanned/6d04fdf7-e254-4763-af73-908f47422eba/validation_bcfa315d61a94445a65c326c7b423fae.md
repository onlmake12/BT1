### Title
Missing `is_active()` Guard in `CKBHandler::poll()` Allows Protocol Handlers to Execute When Network Is Disabled — (`File: network/src/protocols/mod.rs`)

---

### Summary

`NetworkState::active` is the CKB analog of a "paused" flag. When a node operator calls `set_network_active(false)` via RPC, the intent is to halt all P2P network message processing. Every protocol callback in `CKBHandler` checks `is_active()` before proceeding — except `poll()`, which runs unconditionally. This allows all registered protocol handlers to continue executing their periodic background tasks even when the network is explicitly disabled.

---

### Finding Description

`NetworkState` holds an `AtomicBool` field `active`, initialized to `true`. [1](#0-0) 

The RPC method `set_network_active(false)` stores `false` into this flag: [2](#0-1) 

`CKBHandler` is the `ServiceProtocol` implementation that proxies all tentacle P2P protocol events to CKB's internal handlers. Four of its five event callbacks guard on `is_active()`:

- `connected()` — checks `is_active()` at line 327
- `disconnected()` — checks `is_active()` at line 351
- `received()` — checks `is_active()` at line 366
- `notify()` — checks `is_active()` at line 387 [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

However, `poll()` — the fifth callback, called periodically by the tentacle framework for each registered protocol — contains **no `is_active()` check**:

```rust
async fn poll(&mut self, context: &mut ProtocolContext) -> Option<()> {
    let nc = DefaultCKBProtocolContext {
        proto_id: self.proto_id,
        network_state: Arc::clone(&self.network_state),
        p2p_control: context.control().to_owned().into(),
        async_p2p_control: context.control().to_owned(),
    };
    self.handler.poll(Arc::new(nc)).await
}
``` [7](#0-6) 

`poll()` is invoked by the tentacle runtime on a timer for every active protocol session. Protocol handlers use it to drive outbound behavior: sending block-fetch requests (sync protocol), broadcasting transactions (relay protocol), and probing new peers (discovery protocol). All of these continue to fire even after `set_network_active(false)`.

---

### Impact Explanation

When a node operator disables the network (e.g., in response to an ongoing attack, for maintenance, or to isolate the node), the `poll()` callbacks of all registered protocol handlers continue to execute. This means:

1. The sync protocol can still send `GetHeaders`/`GetBlocks` requests to connected peers.
2. The relay protocol can still broadcast pending transactions.
3. The discovery protocol can still probe and attempt to connect to new peers.

The operator's intent — to halt all P2P activity — is only partially honored. Inbound message processing is suppressed, but outbound, timer-driven activity is not. This is a direct violation of the documented semantics of `set_network_active`: "Disable/enable **all** p2p network activity." [8](#0-7) 

---

### Likelihood Explanation

The `set_network_active(false)` RPC is a legitimate operator tool, documented and exposed in the Net RPC module. Any operator who uses it to isolate their node during an incident will unknowingly leave `poll()`-driven outbound behavior active. The omission is structural — every other callback has the guard, `poll()` does not — and will affect every deployment that uses this RPC.

---

### Recommendation

Add the `is_active()` guard at the top of `CKBHandler::poll()`, consistent with all other callbacks:

```rust
async fn poll(&mut self, context: &mut ProtocolContext) -> Option<()> {
    if !self.network_state.is_active() {
        return None;
    }
    let nc = DefaultCKBProtocolContext {
        proto_id: self.proto_id,
        network_state: Arc::clone(&self.network_state),
        p2p_control: context.control().to_owned().into(),
        async_p2p_control: context.control().to_owned(),
    };
    self.handler.poll(Arc::new(nc)).await
}
``` [7](#0-6) 

---

### Proof of Concept

1. Start a CKB node with the Net RPC module enabled.
2. Call `set_network_active(false)` via RPC.
3. Observe via logging or network capture that the node continues to emit outbound P2P messages (e.g., `GetHeaders`, transaction relay, discovery probes) on the timer interval defined by each protocol's `poll()` implementation.
4. Contrast with `received()`, `connected()`, `disconnected()`, and `notify()` — all of which are silenced by the `is_active()` guard.

The root cause is the missing guard in `network/src/protocols/mod.rs` at the `poll()` function, which is structurally inconsistent with every other callback in the same `CKBHandler` implementation. [9](#0-8)

### Citations

**File:** network/src/network.rs (L87-87)
```rust
    pub(crate) active: AtomicBool,
```

**File:** network/src/network.rs (L1589-1591)
```rust
    pub fn set_active(&self, active: bool) {
        self.network_state.active.store(active, Ordering::Release);
    }
```

**File:** network/src/protocols/mod.rs (L309-407)
```rust
impl ServiceProtocol for CKBHandler {
    async fn init(&mut self, context: &mut ProtocolContext) {
        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        self.handler.init(Arc::new(nc)).await;
    }

    async fn connected(&mut self, context: ProtocolContextMutRef<'_>, version: &str) {
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(context.session.id) {
                peer.protocols.insert(self.proto_id, version.to_owned());
            }
        });

        if !self.network_state.is_active() {
            return;
        }

        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        let peer_index = context.session.id;

        self.handler
            .connected(Arc::new(nc), peer_index, version)
            .await;
    }

    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(context.session.id) {
                peer.protocols.remove(&self.proto_id);
            }
        });

        if !self.network_state.is_active() {
            return;
        }

        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        let peer_index = context.session.id;
        self.handler.disconnected(Arc::new(nc), peer_index).await;
    }

    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        if !self.network_state.is_active() {
            return;
        }

        trace!(
            "[received message]: {}, {}, length={}",
            self.proto_id,
            context.session.id,
            data.len()
        );
        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        let peer_index = context.session.id;
        self.handler.received(Arc::new(nc), peer_index, data).await;
    }

    async fn notify(&mut self, context: &mut ProtocolContext, token: u64) {
        if !self.network_state.is_active() {
            return;
        }
        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        self.handler.notify(Arc::new(nc), token).await;
    }

    async fn poll(&mut self, context: &mut ProtocolContext) -> Option<()> {
        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        self.handler.poll(Arc::new(nc)).await
    }
```

**File:** rpc/src/module/net.rs (L389-393)
```rust
    /// Disable/enable all p2p network activity
    ///
    /// ## Params
    ///
    /// * `state` - true to enable networking, false to disable
```
