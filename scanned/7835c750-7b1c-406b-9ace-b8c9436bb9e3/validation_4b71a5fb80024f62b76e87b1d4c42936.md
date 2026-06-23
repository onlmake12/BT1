### Title
`set_ban` RPC `ban_until` Semantic Mismatch Causes All Peer Bans to Expire ~60 Years in the Future — (`network/src/network.rs`, `network/src/peer_store/peer_store_impl.rs`)

---

### Summary

The `set_ban` RPC correctly computes an absolute `ban_until` timestamp and passes it to `NetworkController::ban()`. However, `NetworkController::ban()` forwards that value directly to `PeerStore::ban_network()`, which treats its second parameter as a **relative duration** (`timeout_ms`) and adds the current time again. The result is that every ban expires at approximately `2 × now_ms` (roughly year 2078), regardless of what the operator specified. The `ban_time` and `absolute` parameters are effectively ignored.

---

### Finding Description

**Layer 1 — RPC handler** (`rpc/src/module/net.rs`, lines 691–727):

The `set_ban` implementation correctly computes an absolute expiry timestamp:

```rust
let ban_until = if absolute.unwrap_or(false) {
    ban_time.unwrap_or_default().into()          // raw absolute timestamp
} else {
    unix_time_as_millis()
        + ban_time
            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
            .value()                              // now + relative duration
};
self.network_controller
    .ban(ip_network, ban_until, reason.unwrap_or_default());
```

In both branches, `ban_until` is an absolute millisecond timestamp. It is passed to `NetworkController::ban()`.

**Layer 2 — NetworkController** (`network/src/network.rs`, lines 1422–1428):

```rust
pub fn ban(&self, address: IpNetwork, ban_until: u64, ban_reason: String) {
    self.disconnect_peers_in_ip_range(address, &ban_reason);
    self.network_state
        .peer_store
        .lock()
        .ban_network(address, ban_until, ban_reason)   // passes ban_until as-is
}
```

The parameter is named `ban_until` (absolute timestamp) and is forwarded unchanged.

**Layer 3 — PeerStore** (`network/src/peer_store/peer_store_impl.rs`, lines 294–303):

```rust
pub(crate) fn ban_network(&mut self, network: IpNetwork, timeout_ms: u64, ban_reason: String) {
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let ban_addr = BannedAddr {
        address: network,
        ban_until: now_ms + timeout_ms,   // treats second arg as relative duration!
        created_at: now_ms,
        ban_reason,
    };
    self.mut_ban_list().ban(ban_addr);
}
```

`ban_network` names its parameter `timeout_ms` and **always adds `now_ms`** to it. When the value it receives is already an absolute timestamp (≈ 1.7 × 10¹² ms in 2024), the stored `ban_until` becomes `now_ms + ban_until_absolute ≈ 2 × now_ms`, which corresponds to approximately year 2078.

The semantic contract is broken at the `NetworkController::ban()` → `ban_network()` boundary: the caller passes an absolute timestamp, the callee treats it as a relative duration.

---

### Impact Explanation

Every call to the `set_ban` RPC results in a ban that expires approximately 60 years in the future, regardless of the `ban_time` or `absolute` values supplied. Concretely:

- `set_ban("1.2.3.4", "insert", "0x15180" /* 1 day */, false, null)` → ban expires ~year 2078, not 24 hours later.
- `set_ban("1.2.3.4", "insert", "0x1ac89236180" /* absolute */, true, null)` → ban expires at `now_ms + absolute_ts`, not at `absolute_ts`.

A node operator who wants to temporarily ban a misbehaving peer (e.g., one sending invalid blocks or spam transactions) cannot do so: the ban is effectively permanent. Conversely, an operator who wants to set a long-term ban has no reliable way to verify the actual expiry. The `absolute` parameter's entire purpose is negated.

**Impact:** Low (local operator misconfiguration; no direct consensus or fund-safety impact)
**Likelihood:** High (every invocation of `set_ban` with any `ban_time` is affected)

---

### Likelihood Explanation

The `set_ban` RPC is a standard node-management operation documented in the CKB RPC README. Any node operator who calls it to manage peer bans — a routine operation for dealing with misbehaving peers — will silently receive incorrect behavior. There is no error returned; the call succeeds and `get_banned_addresses` will show a `ban_until` far in the future, which may not be immediately noticed.

---

### Recommendation

`NetworkController::ban()` should subtract `now_ms` before forwarding to `ban_network()`, converting the absolute timestamp back to a relative duration:

```rust
pub fn ban(&self, address: IpNetwork, ban_until: u64, ban_reason: String) {
    self.disconnect_peers_in_ip_range(address, &ban_reason);
    let now_ms = ckb_systemtime::unix_time_as_millis();
    let timeout_ms = ban_until.saturating_sub(now_ms);
    self.network_state
        .peer_store
        .lock()
        .ban_network(address, timeout_ms, ban_reason)
}
```

Alternatively, change `ban_network` to accept an absolute `ban_until` and store it directly, removing the `now_ms +` addition.

---

### Proof of Concept

1. Start a CKB node.
2. Call `set_ban` with a 1-hour ban:
   ```json
   {"method":"set_ban","params":["1.2.3.4","insert","0xD693A400",false,null]}
   ```
   (`0xD693A400` = 3,600,000 ms = 1 hour)
3. Call `get_banned_addresses` and inspect `ban_until`.
4. Observe that `ban_until ≈ 2 × unix_time_as_millis() + 3_600_000`, placing expiry around year 2078 instead of 1 hour from now.

**Root cause trace:**

- `set_ban` computes `ban_until = now_ms + 3_600_000` ≈ `1_718_003_600_000` [1](#0-0) 
- `NetworkController::ban()` passes this value as-is to `ban_network()` [2](#0-1) 
- `ban_network()` stores `ban_until = now_ms + 1_718_003_600_000 ≈ 3_436_003_600_000` ms ≈ year 2078 [3](#0-2)

### Citations

**File:** rpc/src/module/net.rs (L706-714)
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
```

**File:** network/src/network.rs (L1422-1428)
```rust
    pub fn ban(&self, address: IpNetwork, ban_until: u64, ban_reason: String) {
        self.disconnect_peers_in_ip_range(address, &ban_reason);
        self.network_state
            .peer_store
            .lock()
            .ban_network(address, ban_until, ban_reason)
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
