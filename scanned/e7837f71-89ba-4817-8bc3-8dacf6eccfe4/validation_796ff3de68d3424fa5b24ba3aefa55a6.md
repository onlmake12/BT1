Based on my analysis of the actual code, this vulnerability is **real and reachable**.

---

### Title
Score Reset via Discovery Re-advertisement Erases Peer Penalties — (`network/src/peer_store/addr_manager.rs`, `network/src/peer_store/peer_store_impl.rs`)

### Summary

`AddrManager::add` unconditionally overwrites the entire `AddrInfo` struct (including `score`) when the incoming `last_connected_at_ms` is `>=` the stored value. Since `add_addr` always passes `last_connected_at_ms=0`, and a peer that was only discovered (never connected) also stores `last_connected_at_ms=0`, the condition `0 >= 0` is satisfied and the score is silently reset to `default_score=100`.

### Finding Description

**Step 1 — Initial add via discovery:**

`add_addr` in `peer_store_impl.rs` always constructs `AddrInfo::new(addr, 0, default_score, flags)`: [1](#0-0) 

This stores the entry with `last_connected_at_ms=0` and `score=100`.

**Step 2 — Score penalty via `report`:**

`report` mutates only the `score` field of the existing `AddrInfo` in-place: [2](#0-1) 

After a bad-behavior report, the entry has `last_connected_at_ms=0`, `score=60` (example, above `ban_score=40`).

**Step 3 — Attacker sends a `Nodes` discovery message:**

Any connected peer can send a `DiscoveryMessage::Nodes` containing arbitrary addresses. The handler calls `add_new_addrs`: [3](#0-2) 

Which calls `peer_store.add_addr(addr, flags)` for each address: [4](#0-3) 

**Step 4 — Overwrite condition in `AddrManager::add`:**

`add` checks `addr_info.last_connected_at_ms >= exist_last_connected_at_ms`. With both values at `0`, the condition `0 >= 0` is `true`, and the **entire entry** is replaced with the new `AddrInfo` carrying `score=default_score=100`: [5](#0-4) 

The comment says "Get time earlier than record time, return directly" — but it does **not** preserve the existing score when overwriting.

### Impact Explanation

A misbehaving peer whose score has been reduced (but not yet below `ban_score=40`) can have any relay peer re-advertise its address via the discovery protocol. This resets its score to `default_score=100`, erasing accumulated penalties. The peer can repeat this indefinitely to avoid ever reaching the ban threshold.

`ban_score=40`, `default_score=100`: [6](#0-5) 

### Likelihood Explanation

The attack requires only a standard P2P connection and sending a `Nodes` discovery message — no special privileges. The condition is deterministic (`0 >= 0`), not probabilistic. Any peer that was added via discovery (never directly connected) is vulnerable to score reset.

### Recommendation

In `AddrManager::add`, when an existing entry is found and the timestamp condition is satisfied, **preserve the existing score** rather than overwriting it with the incoming value:

```rust
if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
    let existing_score = self.id_to_info.get(&id).expect("must exists").score;
    addr_info.random_id_pos = random_id_pos;
    addr_info.score = existing_score; // preserve penalty
    self.id_to_info.insert(id, addr_info);
}
```

### Proof of Concept

1. Call `peer_store.add_addr(addr, flags)` → entry stored with `score=100`, `last_connected_at_ms=0`
2. Call `peer_store.report(&addr, Behaviour::UnexpectedMessage)` → score drops to e.g. 60
3. Any connected peer sends `DiscoveryMessage::Nodes` containing `addr`
4. `add_new_addrs` → `add_addr` → `AddrManager::add` with new `AddrInfo(last_connected_at_ms=0, score=100)`
5. Condition `0 >= 0` → true → entry overwritten → `score=100` again
6. Assert `peer_store.addr_manager().get(&addr).unwrap().score == 60` — **fails**, score is 100 [7](#0-6)

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L76-79)
```rust
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
```

**File:** network/src/peer_store/peer_store_impl.rs (L153-167)
```rust
    pub fn report(&mut self, addr: &Multiaddr, behaviour: Behaviour) -> ReportResult {
        if let Some(peer_addr) = self.addr_manager.get_mut(addr) {
            let score = peer_addr.score.saturating_add(behaviour.score());
            peer_addr.score = score;
            if score < self.score_config.ban_score {
                self.ban_addr(
                    addr,
                    self.score_config.ban_timeout_ms,
                    format!("report behaviour {behaviour:?}"),
                );
                return ReportResult::Banned;
            }
        }
        ReportResult::Ok
    }
```

**File:** network/src/protocols/discovery/mod.rs (L205-205)
```rust
                                self.addr_mgr.add_new_addrs(session.id, addrs);
```

**File:** network/src/protocols/discovery/mod.rs (L354-356)
```rust
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
```

**File:** network/src/peer_store/addr_manager.rs (L22-34)
```rust
    pub fn add(&mut self, mut addr_info: AddrInfo) {
        if let Some(&id) = self.addr_to_id.get(&addr_info.addr) {
            let (exist_last_connected_at_ms, random_id_pos) = {
                let info = self.id_to_info.get(&id).expect("must exists");
                (info.last_connected_at_ms, info.random_id_pos)
            };
            // Get time earlier than record time, return directly
            if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
                addr_info.random_id_pos = random_id_pos;
                self.id_to_info.insert(id, addr_info);
            }
            return;
        }
```

**File:** network/src/peer_store/mod.rs (L51-58)
```rust
impl Default for PeerScoreConfig {
    fn default() -> Self {
        PeerScoreConfig {
            default_score: 100,
            ban_score: 40,
            ban_timeout_ms: 24 * 3600 * 1000, // 1 day
        }
    }
```
