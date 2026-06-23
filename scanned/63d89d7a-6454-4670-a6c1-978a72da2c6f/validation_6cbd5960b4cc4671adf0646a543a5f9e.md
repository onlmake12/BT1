### Title
Ban Duration Can Be Decreased by Triggering Automatic Re-ban — (`File: network/src/peer_store/ban_list.rs`)

---

### Summary

`BanList::ban()` unconditionally overwrites an existing ban entry for an IP/network with a new one, regardless of whether the new `ban_until` timestamp is earlier than the existing one. This mirrors the `AssetLock` vulnerability class exactly: a "lock end time" (here, `ban_until`) can be silently decreased when an update is applied without enforcing monotonicity.

---

### Finding Description

`BanList::ban()` stores banned addresses in a `HashMap<IpNetwork, BannedAddr>` and inserts new entries with a plain `HashMap::insert`, which unconditionally replaces any existing entry for the same key:

```rust
pub fn ban(&mut self, banned_addr: BannedAddr) {
    self.inner.insert(banned_addr.address, banned_addr);
    ...
}
``` [1](#0-0) 

There are two callers that produce bans with different durations:

1. **`set_ban` RPC** (`rpc/src/module/net.rs`): an operator can ban a peer for an arbitrary duration (e.g., 24 hours or more). [2](#0-1) 

2. **`ban_session`** (`network/src/network.rs`): called automatically when a peer sends a malformed P2P message, using the fixed constant `BAD_MESSAGE_BAN_TIME = 5 minutes`. [3](#0-2) 

`ban_session` calls `ban_addr` → `ban_network` → `mut_ban_list().ban(ban_addr)`, which constructs a `BannedAddr` with `ban_until = now_ms + timeout_ms` (5 minutes) and inserts it, overwriting any existing longer ban. [4](#0-3) 

The `BAD_MESSAGE_BAN_TIME` constant is 5 minutes, far shorter than the 24-hour default ban applied by `set_ban`: [5](#0-4) 

---

### Impact Explanation

A peer that has been manually banned for a long duration (e.g., 24 hours via `set_ban`) can reduce its effective ban to 5 minutes by deliberately triggering an automatic re-ban. After the 5-minute ban expires, the peer can reconnect and resume malicious activity, defeating the operator's intent to exclude the peer for a longer period.

---

### Likelihood Explanation

The attack requires the peer to be connected at the time the long-duration ban is applied (or to reconnect before the ban check takes effect, since `disconnect_with_message` is asynchronous). Once connected, sending a single malformed P2P message is trivial and sufficient to trigger `ban_session`. This is a realistic, low-effort action for any connected peer.

---

### Recommendation

In `BanList::ban()`, when an entry already exists for the given `IpNetwork`, update it only if the new `ban_until` is greater than the existing one:

```rust
pub fn ban(&mut self, banned_addr: BannedAddr) {
    let entry = self.inner.entry(banned_addr.address);
    match entry {
        Entry::Occupied(mut e) => {
            if banned_addr.ban_until > e.get().ban_until {
                e.insert(banned_addr);
            }
        }
        Entry::Vacant(e) => { e.insert(banned_addr); }
    }
    ...
}
```

This ensures that a shorter automatic ban can never overwrite a longer manually-set ban, analogous to using `max(current_lockEndTime, new_lockEndTime)` in the `AssetLock` fix.

---

### Proof of Concept

1. Operator calls `set_ban("192.168.0.2", "insert", 86400000, false, "persistent ban")` — bans the peer for 24 hours.
2. The peer is still connected (disconnect via `disconnect_with_message` is asynchronous).
3. The peer sends a malformed P2P message (e.g., a truncated `SyncMessage`).
4. The node calls `ban_session(peer, BAD_MESSAGE_BAN_TIME=5min, ...)` → `ban_addr` → `ban_network` → `BanList::ban()`.
5. `BanList::ban()` calls `self.inner.insert(192.168.0.2/32, BannedAddr { ban_until: now + 5min })`, overwriting the 24-hour entry.
6. `get_banned_addresses` now shows `ban_until = now + 5 minutes` instead of `now + 24 hours`.
7. After 5 minutes the peer reconnects and resumes activity. [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** rpc/src/module/net.rs (L706-717)
```rust
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
```

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

**File:** network/src/peer_store/peer_store_impl.rs (L286-303)
```rust
    pub(crate) fn ban_addr(&mut self, addr: &Multiaddr, timeout_ms: u64, ban_reason: String) {
        if let Some(addr) = multiaddr_to_socketaddr(addr) {
            let network = ip_to_network(addr.ip());
            self.ban_network(network, timeout_ms, ban_reason)
        }
        self.addr_manager.remove(addr);
    }

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

**File:** util/constant/src/sync.rs (L59-65)
```rust
/// Default ban time for message
// ban time
// 5 minutes
pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);
/// Default ban time for sync useless
// 10 minutes, peer have no common ancestor block
pub const SYNC_USELESS_BAN_TIME: Duration = Duration::from_secs(10 * 60);
```
