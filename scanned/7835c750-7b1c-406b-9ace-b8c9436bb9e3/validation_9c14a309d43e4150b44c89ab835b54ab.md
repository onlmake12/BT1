### Title
Test-Only IP Bypass in Peer Address Selection Allows Deduplication Evasion - (File: `network/src/peer_store/addr_manager.rs`)

### Summary
The `fetch_random` function in `AddrManager` contains an explicitly labeled test workaround (`is_test_ip`) that bypasses the per-IP deduplication guard for loopback (`127.x.x.x`) and unspecified (`0.0.0.0`) addresses. This code is present in production and is reachable via the P2P discovery protocol. A remote peer can advertise many loopback/unspecified addresses through discovery, causing the node to return all of them from `fetch_random` without deduplication, wasting outbound connection slots and degrading peer diversity.

### Finding Description
In `network/src/peer_store/addr_manager.rs`, the `fetch_random` method — which selects candidate peer addresses for outbound connections — contains the following logic:

```rust
// A trick to make our tests work
// TODO remove this after fix the network tests.
let is_test_ip = ip.is_unspecified() || ip.is_loopback();
if (is_test_ip || is_unique_ip)
    && addr_info.is_connectable(now_ms)
    && filter(&addr_info)
{
    duplicate_ips.insert(ip);
    addr_infos.push(addr_info);
}
```

The comment explicitly identifies this as a test workaround. The `is_unique_ip` guard is the production deduplication mechanism: it ensures at most one address per IP is returned, providing Sybil resistance at the address-selection layer. The `is_test_ip` short-circuit completely bypasses this guard for loopback and unspecified IPs, allowing arbitrarily many such addresses to be returned in a single `fetch_random` call.

`fetch_random` is called in production from `peer_store_impl.rs` and from the discovery protocol handler (`protocols/discovery/mod.rs`) to populate outbound connection candidates.

### Impact Explanation
A remote peer participating in the CKB discovery protocol can advertise a large number of loopback addresses (e.g., `127.0.0.1:8114`, `127.0.0.1:8115`, …) or unspecified addresses. These are stored in the local peer store. When `fetch_random` is subsequently called to select peers to connect to, all injected loopback/unspecified addresses bypass the `is_unique_ip` deduplication and are returned together. The node then wastes outbound connection slots attempting to reach these addresses (which either fail or loop back to itself), crowding out legitimate peer addresses and degrading peer diversity. Sustained injection degrades the node's ability to maintain a healthy, diverse peer set, weakening its resistance to network-level partitioning.

### Likelihood Explanation
The CKB discovery protocol is reachable by any unprivileged peer. Advertising addresses through discovery is a standard, unauthenticated operation. No special privileges, keys, or majority hashpower are required. An attacker needs only to connect to the target node and send discovery messages containing many loopback/unspecified addresses.

### Recommendation
Remove the `is_test_ip` workaround and the associated `TODO` comment from production code. Fix the underlying network tests to not rely on loopback/unspecified address bypass. Loopback and unspecified addresses should be subject to the same `is_unique_ip` deduplication as all other addresses, or should be filtered out entirely from the peer store in production builds.

### Proof of Concept
1. Connect to a CKB node as a peer via the discovery protocol.
2. Send discovery `Nodes` messages advertising a large number of addresses of the form `127.0.0.1:N` for many distinct ports `N`.
3. These addresses pass `is_connectable` checks (if timestamps are set appropriately) and are stored in the peer store.
4. When the node calls `fetch_random` (e.g., during periodic peer refresh), all injected `127.0.0.1:N` addresses satisfy `is_test_ip = true`, bypassing `is_unique_ip`, and are returned together.
5. The node attempts outbound connections to all of them, exhausting connection budget and reducing legitimate peer connectivity. [1](#0-0) [2](#0-1)

### Citations

**File:** network/src/peer_store/addr_manager.rs (L44-48)
```rust
    /// Randomly return addrs that worth to try or connect.
    pub fn fetch_random<F>(&mut self, count: usize, filter: F) -> Vec<AddrInfo>
    where
        F: Fn(&AddrInfo) -> bool,
    {
```

**File:** network/src/peer_store/addr_manager.rs (L62-72)
```rust
                    let is_unique_ip = !duplicate_ips.contains(&ip);
                    // A trick to make our tests work
                    // TODO remove this after fix the network tests.
                    let is_test_ip = ip.is_unspecified() || ip.is_loopback();
                    if (is_test_ip || is_unique_ip)
                        && addr_info.is_connectable(now_ms)
                        && filter(&addr_info)
                    {
                        duplicate_ips.insert(ip);
                        addr_infos.push(addr_info);
                    }
```
