### Title
O(n) Exhaustive Linear Scan in `BanList::is_ip_banned_until` on Unbounded Ban List Causes Connection-Handling DoS — (File: `network/src/peer_store/ban_list.rs`)

---

### Summary

`BanList::is_ip_banned_until` performs an unconditional O(n) linear scan through every entry in the ban list to check subnet containment. The ban list (`BanList.inner`) has no size cap and is never proactively bounded — cleanup only fires every 1024 inserts. The `set_ban` RPC endpoint imposes no rate limit and no maximum list size. A local RPC caller (explicitly in scope) can insert arbitrarily many distinct subnet entries, after which every inbound TCP connection and every discovery-address ingestion triggers the full O(n) scan, degrading or stalling connection handling.

---

### Finding Description

`BanList` in `network/src/peer_store/ban_list.rs` stores banned networks in a `HashMap<IpNetwork, BannedAddr>`:

```rust
pub struct BanList {
    inner: HashMap<IpNetwork, BannedAddr>,
    insert_count: usize,
}
``` [1](#0-0) 

The membership check `is_ip_banned_until` first does an O(1) exact-key lookup for a single-host entry, but then **always falls through to a full linear scan** of every entry to handle subnet containment:

```rust
fn is_ip_banned_until(&self, ip: IpAddr, now_ms: u64) -> bool {
    let ip_network = ip_to_network(ip);
    if let Some(banned_addr) = self.inner.get(&ip_network)
        && banned_addr.ban_until.gt(&now_ms)
    {
        return true;
    }

    self.inner.iter().any(|(ip_network, banned_addr)| {
        banned_addr.ban_until.gt(&now_ms) && ip_network.contains(ip)
    })
}
``` [2](#0-1) 

The only size-reduction mechanism is `clear_expires()`, which is triggered only every `CLEAR_INTERVAL_COUNTER` (1024) inserts and only removes **expired** entries:

```rust
pub(crate) const CLEAR_INTERVAL_COUNTER: usize = 1024;
``` [3](#0-2) 

```rust
fn clear_expires(&mut self) {
    let now = unix_time_as_millis();
    self.inner
        .retain(|_, banned_addr| banned_addr.ban_until.gt(&now));
}
``` [4](#0-3) 

If entries are inserted with long (or absolute far-future) ban durations, they are never removed by `clear_expires`, so the list grows without bound.

`is_addr_banned` is called in at least four hot paths in `peer_store_impl.rs`:

- `add_addr` (line 72) — called for every address received in a discovery message
- `add_outbound_addr` (line 104)
- `update_outbound_addr_last_connected_ms` (line 123)
- `is_addr_banned` forwarded from `PeerRegistry::accept_peer` (line 109 of `peer_registry.rs`) — called on **every new inbound TCP session** [5](#0-4) [6](#0-5) 

The `set_ban` RPC handler in `rpc/src/module/net.rs` inserts entries with no rate limit and no cap on list size:

```rust
fn set_ban(
    &self,
    address: String,
    command: String,
    ban_time: Option<Timestamp>,
    absolute: Option<bool>,
    reason: Option<String>,
) -> Result<()> {
    ...
    "insert" => {
        ...
        self.network_controller
            .ban(ip_network, ban_until, reason.unwrap_or_default());
        Ok(())
    }
``` [7](#0-6) 

---

### Impact Explanation

With N entries in the ban list, every inbound connection attempt costs O(N) CPU time in the network event loop. With N = 500,000 distinct /32 or /24 subnet entries (all with far-future expiry), each new connection triggers half a million comparisons before the connection is accepted or rejected. Because `accept_peer` holds the `peer_store` mutex during this scan, all concurrent connection events are serialized behind it. This can cause the node to stop accepting new peers, stall the discovery protocol's address ingestion, and degrade overall P2P responsiveness — a targeted resource-exhaustion DoS on connection handling.

---

### Likelihood Explanation

The `set_ban` RPC is accessible to any process on the same machine (default `127.0.0.1:8114`). A compromised local process, a malicious script, or a misconfigured operator tool can call `set_ban` in a tight loop with distinct /32 addresses (e.g., iterating through `1.0.0.0/8`) with `ban_time` set to a far-future absolute timestamp. There is no server-side rate limit, no maximum list size, and no warning when the list grows large. The attack requires no network access beyond localhost.

---

### Recommendation

1. **Cap the ban list size.** Enforce a hard maximum (e.g., 10,000 entries). On insertion when at capacity, evict the entry with the earliest `ban_until`.
2. **Eliminate the unconditional O(n) subnet scan.** Maintain a separate sorted interval structure (e.g., an interval tree or prefix trie keyed on `IpNetwork`) so subnet containment checks are O(log n) rather than O(n).
3. **Rate-limit `set_ban` RPC calls** or require explicit acknowledgment when the list exceeds a threshold.
4. **Run `clear_expires` on reads** (or on a periodic timer), not only every 1024 inserts.

---

### Proof of Concept

```python
import requests, json

url = "http://127.0.0.1:8114"
headers = {"Content-Type": "application/json"}

# Insert 200,000 distinct /32 subnet bans with a far-future expiry
for i in range(200000):
    a = (i >> 16) & 0xFF
    b = (i >> 8)  & 0xFF
    c = i         & 0xFF
    ip = f"10.{a}.{b}.{c}"
    payload = {
        "id": i, "jsonrpc": "2.0", "method": "set_ban",
        "params": [ip, "insert", "9999999999999", True, "dos"]
    }
    requests.post(url, headers=headers, data=json.dumps(payload))

print("Ban list filled. Every new inbound connection now triggers O(200000) scan.")
```

After this, any new peer connecting to the node causes `accept_peer` → `is_addr_banned` → `is_ip_banned_until` to iterate all 200,000 entries under the `peer_store` mutex, serializing all connection events and effectively stalling the P2P layer. [8](#0-7) [9](#0-8)

### Citations

**File:** network/src/peer_store/ban_list.rs (L10-10)
```rust
pub(crate) const CLEAR_INTERVAL_COUNTER: usize = 1024;
```

**File:** network/src/peer_store/ban_list.rs (L13-16)
```rust
pub struct BanList {
    inner: HashMap<IpNetwork, BannedAddr>,
    insert_count: usize,
}
```

**File:** network/src/peer_store/ban_list.rs (L48-59)
```rust
    fn is_ip_banned_until(&self, ip: IpAddr, now_ms: u64) -> bool {
        let ip_network = ip_to_network(ip);
        if let Some(banned_addr) = self.inner.get(&ip_network)
            && banned_addr.ban_until.gt(&now_ms)
        {
            return true;
        }

        self.inner.iter().any(|(ip_network, banned_addr)| {
            banned_addr.ban_until.gt(&now_ms) && ip_network.contains(ip)
        })
    }
```

**File:** network/src/peer_store/ban_list.rs (L79-83)
```rust
    fn clear_expires(&mut self) {
        let now = unix_time_as_millis();
        self.inner
            .retain(|_, banned_addr| banned_addr.ban_until.gt(&now));
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L71-79)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
```

**File:** network/src/peer_registry.rs (L97-99)
```rust
        if self.get_key_by_peer_id(&peer_id).is_some() {
            return Err(PeerError::PeerIdExists(peer_id).into());
        }
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```

**File:** rpc/src/module/net.rs (L691-717)
```rust
    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()> {
        let ip_network = address.parse().map_err(|_| {
            RPCError::invalid_params(format!(
                "Expected `params[0]` to be a valid IP address, got {address}"
            ))
        })?;

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
