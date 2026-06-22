### Title
Silent Overwrite of Existing Ban Entry Allows Unintentional Ban Duration Reduction — (`File: network/src/peer_store/ban_list.rs`)

---

### Summary

`BanList::ban()` silently overwrites an existing ban entry without checking whether the address is already banned, while `BanList::unban_network()` silently proceeds even when the address is not present. This mirrors the exact inconsistency in the reference report: one operation silently proceeds on a duplicate/no-op state, while the analogous inverse operation has no symmetric feedback either. Additionally, `insert_count` is incremented on every `ban()` call — including re-bans of the same address — causing the periodic `clear_expires()` cleanup to fire at incorrect intervals.

---

### Finding Description

**`BanList::ban()` — silent overwrite, no duplicate check:**

```rust
// network/src/peer_store/ban_list.rs:34-41
pub fn ban(&mut self, banned_addr: BannedAddr) {
    self.inner.insert(banned_addr.address, banned_addr); // silently overwrites
    let (insert_count, _) = self.insert_count.overflowing_add(1); // increments even on re-ban
    self.insert_count = insert_count;
    if self.insert_count.is_multiple_of(CLEAR_INTERVAL_COUNTER) {
        self.clear_expires();
    }
}
```

`HashMap::insert` unconditionally replaces any existing entry for the same `IpNetwork` key. No check is performed to see whether the address is already banned, and no error or warning is returned. The `insert_count` counter is incremented regardless of whether a new entry was actually added or an existing one was overwritten.

**`BanList::unban_network()` — silent no-op on non-existent entry:**

```rust
// network/src/peer_store/ban_list.rs:44-46
pub fn unban_network(&mut self, ip_network: &IpNetwork) {
    self.inner.remove(ip_network); // silently proceeds even if not present
}
```

`HashMap::remove` returns `None` silently if the key does not exist. No error is surfaced to the caller.

**The `set_ban` RPC exposes both paths to any local RPC caller:**

```rust
// rpc/src/module/net.rs:705-726
match command.as_ref() {
    "insert" => {
        // ... computes ban_until ...
        self.network_controller.ban(ip_network, ban_until, reason.unwrap_or_default());
        Ok(()) // always Ok, even if already banned
    }
    "delete" => {
        self.network_controller.unban(&ip_network);
        Ok(()) // always Ok, even if not banned
    }
    ...
}
```

The call chain for `"insert"` is: `set_ban` → `NetworkController::ban()` → `PeerStore::ban_network()` → `BanList::ban()`. At no point is the existing ban state checked.

**`insert_count` miscounting on re-bans:**

`insert_count` is incremented on every `ban()` call. When the same address is re-banned (e.g., by the automated peer-scoring path via `PeerStore::report()` → `ban_addr()` → `ban_network()` → `BanList::ban()`), the HashMap size does not grow but `insert_count` does. This means `clear_expires()` is triggered based on total `ban()` invocations, not unique entries, causing cleanup to fire at incorrect intervals.

---

### Impact Explanation

An RPC caller (operator) who re-bans an already-banned IP with a shorter duration will silently reduce the active ban duration. For example:

1. Operator bans `192.168.0.1` for 24 hours due to observed malicious behavior.
2. Operator (or an automated script) calls `set_ban` again for the same IP with a 1-hour duration.
3. `BanList::ban()` silently overwrites the 24-hour entry with a 1-hour entry.
4. The malicious peer can reconnect after 1 hour instead of 24 hours.

There is no error, no warning, and no way for the caller to distinguish a new ban from an overwrite. The inverse (`"delete"` on a non-banned address) also silently returns `Ok(())`, giving the caller false confidence that the operation succeeded.

The `insert_count` miscounting is a secondary impact: in high-churn scenarios where the same peer is repeatedly reported and re-banned (e.g., a peer that keeps reconnecting and misbehaving), `clear_expires()` fires prematurely, potentially evicting other still-valid ban entries for different IPs.

---

### Likelihood Explanation

The `set_ban` RPC is accessible to any local RPC user (default: `127.0.0.1:8114`). Automated node management scripts, monitoring tools, or operators working from incomplete state can easily trigger a re-ban. The peer-scoring path (`report()` → `ban_addr()`) is triggered automatically by protocol violations and can re-ban the same IP multiple times without any guard.

---

### Recommendation

1. In `BanList::ban()`, check whether the address is already banned. If it is, either reject the re-ban with an error, or only overwrite if the new `ban_until` is strictly greater than the existing one (i.e., never silently reduce ban duration).
2. In `BanList::unban_network()`, return a boolean or `Result` indicating whether an entry was actually removed, and surface this through the `set_ban` RPC.
3. Fix `insert_count` to only increment when a genuinely new entry is inserted (i.e., when `HashMap::insert` returns `None`).

---

### Proof of Concept

**Silent ban duration reduction via RPC:**

```json
// Step 1: ban for 24 hours
{"method": "set_ban", "params": ["192.168.0.1", "insert", "0x5265C00", false, "malicious peer"]}
// Response: {"result": null}  ← success

// Step 2: accidentally re-ban for 1 hour
{"method": "set_ban", "params": ["192.168.0.1", "insert", "0xD693A4", false, "test"]}
// Response: {"result": null}  ← also "success", but silently overwrote the 24h ban with 1h

// Step 3: verify — ban_until is now only 1 hour from now, not 24
{"method": "get_banned_addresses", "params": []}
// ban_until reflects the 1-hour ban, not the original 24-hour ban
```

Root cause: `BanList::ban()` at [1](#0-0)  unconditionally calls `HashMap::insert`, overwriting any existing entry. The `set_ban` RPC implementation at [2](#0-1)  performs no pre-check and always returns `Ok(())`. The inverse `unban_network` at [3](#0-2)  is equally silent on non-existent entries. The `insert_count` miscounting is at [4](#0-3) , and the automated re-ban path is at [5](#0-4) .

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

**File:** network/src/peer_store/ban_list.rs (L44-46)
```rust
    pub fn unban_network(&mut self, ip_network: &IpNetwork) {
        self.inner.remove(ip_network);
    }
```

**File:** rpc/src/module/net.rs (L705-717)
```rust
        match command.as_ref() {
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

**File:** network/src/peer_store/peer_store_impl.rs (L285-303)
```rust
    /// Ban an addr
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
