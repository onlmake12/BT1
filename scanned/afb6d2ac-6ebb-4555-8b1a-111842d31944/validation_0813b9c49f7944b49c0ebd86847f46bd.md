### Title
Non-Monotonic Ban Expiry Allows Shorter Automatic Ban to Overwrite Longer Existing Ban - (`network/src/peer_store/ban_list.rs`)

---

### Summary

`BanList::ban()` uses `HashMap::insert` to unconditionally replace an existing ban entry for an IP network, without checking whether the new `ban_until` timestamp is later than the existing one. An unprivileged P2P peer that can trigger two concurrent automatic ban events of different durations from the same IP can reduce its effective ban window, allowing earlier reconnection than the node operator intends.

---

### Finding Description

`BanList::ban()` stores ban records in a `HashMap<IpNetwork, BannedAddr>`:

```rust
pub fn ban(&mut self, banned_addr: BannedAddr) {
    self.inner.insert(banned_addr.address, banned_addr);
    ...
}
```

`HashMap::insert` replaces any existing entry for the same key unconditionally. [1](#0-0) 

The upstream call site `ban_network` computes `ban_until = now_ms + timeout_ms` and passes the resulting `BannedAddr` directly to `ban()`:

```rust
pub(crate) fn ban_network(&mut self, network: IpNetwork, timeout_ms: u64, ban_reason: String) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let ban_addr = BannedAddr {
        address: network,
        ban_until: now_ms + timeout_ms,
        ...
    };
    self.mut_ban_list().ban(ban_addr);
}
``` [2](#0-1) 

There are two distinct automatic ban durations in the sync subsystem:

- `BAD_MESSAGE_BAN_TIME` = **5 minutes** — applied when a peer sends a malformed sync/relay message. [3](#0-2) 
- `SYNC_USELESS_BAN_TIME` = **10 minutes** — applied when a peer has no common ancestor block. [4](#0-3) 

Both flow through `ban_session` → `ban_addr` → `ban_network` → `ban_list.ban()`. [5](#0-4) 

The malformed-message path is exercised directly in the sync protocol handler: [6](#0-5) 

Because `ban()` never checks `if new_ban_until > existing_ban_until`, a second call with a shorter duration silently replaces the longer ban.

---

### Impact Explanation

A peer banned for 10 minutes (`SYNC_USELESS_BAN_TIME`) can reduce its effective ban to 5 minutes (`BAD_MESSAGE_BAN_TIME`) by triggering a malformed-message ban from a concurrent connection originating from the same IP. After only 5 minutes the peer can reconnect, defeating the intended 10-minute exclusion. At scale this allows a misbehaving peer to maintain near-continuous connectivity despite repeated automatic banning, undermining the node's peer-eviction and DoS-mitigation logic.

---

### Likelihood Explanation

The attacker needs two simultaneous TCP connections from the same IP address — a normal capability for any peer. Connection 1 triggers the longer ban (e.g., by presenting headers with no common ancestor). Connection 2, still alive in the peer registry at the moment the first ban fires, immediately sends a malformed message. Both `ban_session` calls reach `ban_list.ban()` for the same `IpNetwork` key; whichever executes second wins, and the shorter 5-minute duration is the one most likely to land last because the malformed-message path is faster. No privileged access, no key material, and no majority hashpower are required.

---

### Recommendation

Apply the same fix as the NFTX report: only update the stored ban if the incoming `ban_until` is strictly greater than the existing one.

```rust
pub fn ban(&mut self, banned_addr: BannedAddr) {
    let new_until = banned_addr.ban_until;
    let entry = self.inner.entry(banned_addr.address);
    match entry {
        std::collections::hash_map::Entry::Occupied(mut e) => {
            if new_until > e.get().ban_until {
                e.insert(banned_addr);
            }
        }
        std::collections::hash_map::Entry::Vacant(e) => {
            e.insert(banned_addr);
        }
    }
    let (insert_count, _) = self.insert_count.overflowing_add(1);
    self.insert_count = insert_count;
    if self.insert_count.is_multiple_of(CLEAR_INTERVAL_COUNTER) {
        self.clear_expires();
    }
}
```

---

### Proof of Concept

1. Attacker peer at IP `A` opens two simultaneous outbound connections to the victim CKB node: session S1 and session S2.
2. On S1, the peer sends a `GetHeaders` response with headers that share no common ancestor with the node's chain, triggering `ban_peer(S1, SYNC_USELESS_BAN_TIME=10min, ...)`.
3. `ban_session` for S1 fires: `ban_list.ban(BannedAddr { ban_until: now+10min, ... })` is stored. S1 is disconnected.
4. S2 is still alive in the peer registry. The peer immediately sends four zero bytes over S2.
5. The sync handler calls `ban_peer(S2, BAD_MESSAGE_BAN_TIME=5min, ...)`.
6. `ban_session` for S2 fires: `ban_list.ban(BannedAddr { ban_until: now+5min, ... })` — `HashMap::insert` replaces the 10-minute entry with the 5-minute entry. [7](#0-6) 
7. After 5 minutes the peer reconnects successfully, having bypassed the intended 10-minute ban.

### Citations

**File:** network/src/peer_store/ban_list.rs (L34-41)
```rust
    pub fn ban(&mut self, banned_addr: BannedAddr) {
        self.inner.insert(banned_addr.address, banned_addr);
        let (insert_count, _) = self.insert_count.overflowing_add(1);
        self.insert_count = insert_count;
        if self.insert_count.is_multiple_of(CLEAR_INTERVAL_COUNTER) {
            self.clear_expires();
        }
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L294-303)
```rust
    pub(crate) fn ban_network(&mut self, network: IpNetwork, timeout_ms: u64, ban_reason: String) {
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let ban_addr = BannedAddr {
            address: network,
            ban_until: now_ms + timeout_ms,
            created_at: now_ms,
            ban_reason,
        };
        self.mut_ban_list().ban(ban_addr);
    }
```

**File:** util/constant/src/sync.rs (L62-62)
```rust
pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);
```

**File:** util/constant/src/sync.rs (L64-65)
```rust
// 10 minutes, peer have no common ancestor block
pub const SYNC_USELESS_BAN_TIME: Duration = Duration::from_secs(10 * 60);
```

**File:** network/src/network.rs (L241-274)
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
```

**File:** sync/src/synchronizer/mod.rs (L906-913)
```rust
                        nc.ban_peer(
                            peer_index,
                            BAD_MESSAGE_BAN_TIME,
                            String::from(
                                "send us a malformed message: \
                                 too many fields in SendBlock",
                            ),
                        );
```
