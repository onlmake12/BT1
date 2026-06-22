### Title
Missing `is_active()` Guard in `CKBHandler::poll()` Allows Network Activity After `set_network_active(false)` — (`network/src/protocols/mod.rs`)

---

### Summary

`CKBHandler::poll()` is the only protocol callback that does **not** check `NetworkState::is_active()` before delegating to the inner handler. Every other callback — `connected`, `disconnected`, `received`, and `notify` — guards on `is_active()` and returns early when the network is deactivated. `poll()` skips this guard entirely, meaning any protocol handler that uses `poll()` to originate outbound messages continues to do so even after an operator calls `set_network_active(false)`.

---

### Finding Description

The `set_network_active` RPC (documented as "Disable/enable all p2p network activity") sets `NetworkState::active` to `false`. The `CKBHandler` struct, which wraps every registered `CKBProtocolHandler`, enforces this flag in four of its five callbacks: [1](#0-0) 

```rust
// connected — guarded
if !self.network_state.is_active() { return; }
``` [2](#0-1) 

```rust
// disconnected — guarded
if !self.network_state.is_active() { return; }
``` [3](#0-2) 

```rust
// received — guarded
if !self.network_state.is_active() { return; }
``` [4](#0-3) 

```rust
// notify — guarded
if !self.network_state.is_active() { return; }
```

But `poll()` has **no such guard**: [5](#0-4) 

```rust
async fn poll(&mut self, context: &mut ProtocolContext) -> Option<()> {
    let nc = DefaultCKBProtocolContext {
        proto_id: self.proto_id,
        network_state: Arc::clone(&self.network_state),
        p2p_control: context.control().to_owned().into(),
        async_p2p_control: context.control().to_owned(),
    };
    self.handler.poll(Arc::new(nc)).await   // no is_active() check
}
```

The `DefaultCKBProtocolContext` passed into `poll()` carries full send capabilities (`async_send_message`, `async_filter_broadcast`, `async_quick_send_message`, etc.). Any protocol handler that overrides `poll()` to originate outbound messages — a legitimate use of the tentacle `poll` hook — will bypass the deactivation guard entirely.

The `is_active()` flag itself is correctly defined and used everywhere else: [6](#0-5) 

```rust
pub fn is_active(&self) -> bool {
    self.active.load(Ordering::Acquire)
}
```

And `set_active` is the only write path: [7](#0-6) 

```rust
pub fn set_active(&self, active: bool) {
    self.network_state.active.store(active, Ordering::Release);
}
```

The RPC handler calls it unconditionally with no additional enforcement: [8](#0-7) 

```rust
fn set_network_active(&self, state: bool) -> Result<()> {
    self.network_controller.set_active(state);
    Ok(())
}
```

---

### Impact Explanation

When an operator calls `set_network_active(false)` — for example, in response to a discovered consensus bug, a LayerZero-style messaging library upgrade, or an emergency maintenance window — the intent is to halt **all** P2P network activity. The missing guard in `poll()` means any protocol handler that uses `poll()` to send periodic messages (block announcements, transaction relay, ping-like keep-alives, or light-client proofs) continues to originate those messages. This renders the emergency shutdown mechanism partially ineffective: the node appears deactivated to the operator but continues to participate in the network through `poll()`-driven sends.

---

### Likelihood Explanation

The `poll()` hook is a standard tentacle `ServiceProtocol` callback designed for periodic, handler-driven work. The `CKBProtocolHandler` trait exposes it with a default `None` return, but any current or future protocol handler that overrides it to send messages will silently bypass the deactivation guard. The gap is structural and persistent — it exists regardless of which handlers are registered — and is reachable by any local RPC caller with access to `set_network_active`.

---

### Recommendation

Add the same `is_active()` guard to `poll()` that is already present in all other `CKBHandler` callbacks:

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
```

This makes the deactivation guarantee uniform across all five protocol callbacks.

---

### Proof of Concept

1. Operator calls `set_network_active(false)` via RPC. This stores `false` into `NetworkState::active`.
2. The tentacle runtime continues to invoke `CKBHandler::poll()` on all registered protocol handlers at their configured intervals.
3. `CKBHandler::poll()` constructs a `DefaultCKBProtocolContext` with live `p2p_control` and `async_p2p_control` handles and passes it to `self.handler.poll()` — **without checking `is_active()`**.
4. Any handler that calls `nc.async_send_message(...)`, `nc.async_filter_broadcast(...)`, or any other send method inside its `poll()` implementation successfully originates outbound network messages.
5. The operator's intent to halt all P2P activity is violated: `received`, `notify`, `connected`, and `disconnected` are all silenced, but `poll()`-driven sends continue unimpeded.

### Citations

**File:** network/src/protocols/mod.rs (L320-342)
```rust
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
```

**File:** network/src/protocols/mod.rs (L344-363)
```rust
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
```

**File:** network/src/protocols/mod.rs (L365-384)
```rust
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
```

**File:** network/src/protocols/mod.rs (L386-397)
```rust
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
```

**File:** network/src/protocols/mod.rs (L399-407)
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
```

**File:** network/src/network.rs (L535-538)
```rust
    /// Network message processing controller, default is true, if false, discard any received messages
    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }
```

**File:** network/src/network.rs (L1588-1591)
```rust
    /// Change active status, if set false discard any received messages
    pub fn set_active(&self, active: bool) {
        self.network_state.active.store(active, Ordering::Release);
    }
```

**File:** rpc/src/module/net.rs (L772-775)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
    }
```
