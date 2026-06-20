### Title
`set_network_active(false)` Does Not Suppress Protocol `poll` Callbacks, Allowing Continued P2P Activity After Network Disable - (`network/src/protocols/mod.rs`)

---

### Summary

When a local RPC user calls `set_network_active(false)` to disable all P2P network activity, the `CKBHandler::poll` method in `network/src/protocols/mod.rs` does **not** check `is_active()`, while every other protocol lifecycle callback (`connected`, `disconnected`, `received`, `notify`) does. This is a direct analog to the Morpho bug: a feature-disable flag is not checked in one specific code path, allowing the disabled feature to continue operating through that path.

---

### Finding Description

`CKBHandler` is the tentacle `ServiceProtocol` adapter that wraps every CKB protocol handler. It consistently guards all lifecycle callbacks with an `is_active()` check:

- `connected` (line 327): `if !self.network_state.is_active() { return; }`
- `disconnected` (line 351): `if !self.network_state.is_active() { return; }`
- `received` (line 366): `if !self.network_state.is_active() { return; }`
- `notify` (line 387): `if !self.network_state.is_active() { return; }`

But `poll` (lines 399–407) has **no such guard**:

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

The `poll` method is called by the tentacle runtime on every event-loop tick for protocols that override it. Any protocol handler that overrides `poll` to perform periodic work — sending messages, updating state, initiating outbound requests — will continue to do so even after `set_network_active(false)` is called.

The `set_network_active` RPC is documented as "Disable/enable **all** p2p network activity." The `active` flag is an `AtomicBool` initialized to `true` and toggled by the RPC. The intent is a complete halt of protocol-level activity.

---

### Impact Explanation

Any protocol handler that overrides `CKBProtocolHandler::poll` to perform periodic outbound work (e.g., sending sync requests, broadcasting transactions, issuing light-client proofs) will continue executing that work after the operator calls `set_network_active(false)`. This violates the documented contract of the RPC and can:

1. Cause the node to continue sending P2P messages when the operator intended a full network halt (e.g., for emergency maintenance or isolation).
2. Allow a protocol to initiate new outbound connections or message flows that bypass the disable intent.
3. Create an inconsistent state where inbound processing is halted but outbound periodic work continues, potentially confusing peer state machines.

---

### Likelihood Explanation

The `set_network_active` RPC is a supported local operator action explicitly listed in the RPC documentation and accessible to any local RPC user. The missing guard is a straightforward omission — every other callback has it, `poll` does not. Any operator relying on `set_network_active(false)` for a clean network halt will be silently affected.

---

### Recommendation

Add the same `is_active()` guard to `CKBHandler::poll` that is present in all other lifecycle callbacks:

```rust
async fn poll(&mut self, context: &mut ProtocolContext) -> Option<()> {
    if !self.network_state.is_active() {
        return None;
    }
    let nc = DefaultCKBProtocolContext { ... };
    self.handler.poll(Arc::new(nc)).await
}
```

---

### Proof of Concept

1. Start a CKB node.
2. Call `set_network_active(false)` via the Net RPC module.
3. Observe that `connected`, `disconnected`, `received`, and `notify` callbacks all return early (guarded by `is_active()`).
4. Observe that `poll` callbacks for any protocol overriding `CKBProtocolHandler::poll` continue to fire and execute their logic, including any outbound message sends via the `nc` context — because `CKBHandler::poll` at lines 399–407 of `network/src/protocols/mod.rs` contains no `is_active()` check.

The root cause is the missing guard at: [1](#0-0) 

compared to the consistent guard present in all sibling callbacks: [2](#0-1) [3](#0-2)

### Citations

**File:** network/src/protocols/mod.rs (L365-368)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        if !self.network_state.is_active() {
            return;
        }
```

**File:** network/src/protocols/mod.rs (L386-389)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, token: u64) {
        if !self.network_state.is_active() {
            return;
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
