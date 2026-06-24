Audit Report

## Title
Inconsistent Address Normalization in `AddrManager::add()` Bypasses Score-Based Peer Banning for WebSocket Peers — (`network/src/peer_store/addr_manager.rs`)

## Summary
`AddrManager::add()` stores peer addresses using the raw `Multiaddr` (including `/ws`, `/wss`, `/memory`, `/tls` suffixes) as the `addr_to_id` key, while `get_mut()`, `get()`, and `remove()` all normalize via `base_addr()` before lookup. For any outbound WebSocket peer whose address is stored with a `/ws` suffix through `add_outbound_addr()`, subsequent calls to `report()` and `ban_addr()` silently fail to find the entry, permanently bypassing score-based banning and creating ghost entries that can never be evicted.

## Finding Description
The root cause is a key asymmetry in `addr_manager.rs`:

- `add()` (L22–42) uses `addr_info.addr` directly as the `addr_to_id` key with no normalization.
- `get_mut()` (L130–137), `get()` (L122–127), and `remove()` (L110–119) all call `base_addr(addr)` before lookup, stripping `/ws`, `/wss`, `/memory`, and `/tls` protocol components.

`base_addr()` is defined in `peer_store/mod.rs` (L92–104) and filters out exactly those transport-layer suffixes.

**Exploit path:**

1. An outbound WebSocket peer connects; the identify protocol fires `add_outbound_addr(context.session.address.clone(), flags)` (`identify/mod.rs` L421–423).
2. `add_outbound_addr()` (`peer_store_impl.rs` L103–114) calls `addr_manager.add(AddrInfo::new(addr, ...))` with the raw session address `/ip4/A.A.A.A/tcp/8114/ws`.
3. `addr_to_id` now maps `/ip4/A.A.A.A/tcp/8114/ws` → ID 0.
4. Peer misbehaves; `report(&addr, behaviour)` is called (`peer_store_impl.rs` L153–167).
5. `report()` calls `addr_manager.get_mut(addr)` → `base_addr()` strips `/ws` → looks up `/ip4/A.A.A.A/tcp/8114` → **NOT FOUND** → returns `None`.
6. Score is never updated; `ban_addr()` is never triggered from `report()`.
7. If `ban_addr()` is called directly (`peer_store_impl.rs` L286–292), `addr_manager.remove(addr)` normalizes → `/ip4/A.A.A.A/tcp/8114` → **NOT FOUND** → ghost entry persists. Note: `ban_network()` still adds the IP to the ban list, but only if `ban_addr()` is reached — which it is not via the score path.
8. `check_purge()` (`peer_store_impl.rs` L327–404) iterates `addrs_iter()` (which yields raw `/ws` addresses from `id_to_info`) and calls `addr_manager.remove(key)` on them — same normalization mismatch — ghost entries can never be evicted by purge either.

**Existing checks are insufficient:** `ban_list.is_addr_banned()` in `add_outbound_addr()` only prevents re-adding a banned address; it does not compensate for the lookup failure in `get_mut()`. The IP-based ban path in `ban_addr()` is only reached if `report()` successfully decrements the score below `ban_score`, which never happens for `/ws`-stored addresses.

## Impact Explanation
The score-based banning mechanism is completely bypassed for any peer connecting via WebSocket. Misbehaviors that should reduce score and eventually trigger a ban are silently ignored. Ghost entries persist indefinitely in `addr_manager`, are returned by `fetch_random()`, `fetch_addrs_to_attempt()`, and `fetch_addrs_to_feeler()`, and cannot be evicted by `check_purge()`. This constitutes a suboptimal and broken implementation of the CKB peer store's security accounting mechanism, matching **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**, with a secondary fit to **Low (501–2000 points): Any other important performance improvements for CKB** due to the ghost-entry inflation of `ADDR_COUNT_LIMIT`.

## Likelihood Explanation
WebSocket transport is a first-class supported protocol in CKB's network stack — `base_addr()` explicitly enumerates `Protocol::Ws` as a strippable suffix, confirming it is expected in production addresses. Any unprivileged peer that connects outbound via WebSocket automatically triggers `add_outbound_addr()` with a `/ws`-suffixed address during the identify handshake. No special configuration, privilege, or victim mistake is required. The condition is met by every WebSocket outbound connection.

## Recommendation
Normalize the address in `add()` using `base_addr()` before inserting into `addr_to_id`, consistent with how `remove()`, `get()`, and `get_mut()` operate:

```rust
pub fn add(&mut self, mut addr_info: AddrInfo) {
    let normalized = base_addr(&addr_info.addr);
    addr_info.addr = normalized.clone();
    if let Some(&id) = self.addr_to_id.get(&normalized) {
        ...
        return;
    }
    self.addr_to_id.insert(normalized, id);
    ...
}
```

This ensures a single canonical key is used across all operations, eliminating ghost entries and the score-update bypass.

## Proof of Concept

```
1. Node A connects to victim CKB node via WebSocket.
   Session address: /ip4/A.A.A.A/tcp/8114/ws

2. Identify protocol fires → add_outbound_addr(/ip4/A.A.A.A/tcp/8114/ws) called.
   addr_to_id stores: "/ip4/A.A.A.A/tcp/8114/ws" → ID 0  (raw, no normalization)

3. Node A sends malformed messages, triggering report(addr, Behaviour::UnexpectedMessage).
   report() calls addr_manager.get_mut("/ip4/A.A.A.A/tcp/8114/ws")
   → base_addr() strips /ws → looks up "/ip4/A.A.A.A/tcp/8114" → NOT FOUND → None.
   Score is never decremented. ban_addr() is never called. Peer is never banned.

4. check_purge() iterates addrs_iter(), finds "/ip4/A.A.A.A/tcp/8114/ws" as non-connectable,
   calls addr_manager.remove("/ip4/A.A.A.A/tcp/8114/ws")
   → base_addr() → "/ip4/A.A.A.A/tcp/8114" → NOT FOUND.
   Ghost entry persists indefinitely, counted toward ADDR_COUNT_LIMIT = 16384,
   and continues to be returned by fetch_random() / fetch_addrs_to_attempt().

Unit test plan: construct a PeerStore, call add_outbound_addr with a /ws address,
call report() with the same address repeatedly until score should reach ban_score,
assert that get() on the address returns Some (currently returns None — test fails,
proving the bug).
```