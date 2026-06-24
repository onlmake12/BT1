Audit Report

## Title
Off-by-One in `check_purge` Group Eviction Threshold Enables Peer Store Freeze via Crafted Discovery Messages — (File: `network/src/peer_store/peer_store_impl.rs`)

## Summary
The group-based eviction path in `check_purge` uses `> 4` instead of `>= 4` when selecting eviction candidates from network-group buckets. An attacker with a single P2P connection can send crafted `Nodes` discovery messages advertising exactly 4 public IP addresses across 4096 distinct `/16` subnets, filling all 16384 `ADDR_COUNT_LIMIT` slots. Once full, `check_purge` produces zero eviction candidates and permanently returns `PeerStoreError::EvictionFailed`, freezing the peer store and blocking all new peer discovery.

## Finding Description

**Root cause — `network/src/peer_store/peer_store_impl.rs`, line 378:**

The comment at line 338 documents the intent as "more than 4 peer," but the eviction logic at line 378 uses strict `> 4`:

```rust
if addrs.len() > 4 {
    Some(addrs.iter().choose_multiple(..., 2)...)
} else {
    None
}
```

A group of exactly 4 returns `None`, contributing nothing to `candidate_peers`.

**Network group key — `network/src/network_group.rs`, line 28:**

`Group::IP4([bits[0], bits[1]])` groups addresses by the first two octets (i.e., `/16` subnet). An attacker can generate 4096 distinct groups using addresses like `1.0.0.x` through `16.255.0.x`.

**Attack construction:**
- `ADDR_COUNT_LIMIT = 16384` (mod.rs line 26)
- 4096 subnets × 4 addresses each = 16384 entries exactly fills the store
- `AddrInfo::new` inserts with `last_connected_at_ms = 0`, `attempts_count = 0` (types.rs lines 65–76)
- `is_connectable` returns `true` for these fresh entries (types.rs lines 89–105): neither the `attempts_count >= ADDR_MAX_RETRIES` nor the `attempts_count >= ADDR_MAX_FAILURES` condition is met
- Step 1 of `check_purge` (non-connectable eviction) finds nothing
- Step 2: `len = 4096`, `take(len / 2) = take(2048)`. All 2048 taken groups have `len == 4`, none pass `> 4`, so `candidate_peers` is empty → `Err(PeerStoreError::EvictionFailed)` is returned

**Persistence mechanism — `network/src/peer_store/addr_manager.rs`, line 29:**

```rust
if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
    self.id_to_info.insert(id, addr_info);
}
```

Since both old and new entries have `last_connected_at_ms = 0`, the condition `0 >= 0` is true, so re-advertising the same addresses replaces the entry with a fresh one (`attempts_count = 0`), preventing natural expiry via failed dial attempts.

**Error is silently swallowed — `network/src/protocols/discovery/mod.rs`, lines 355–360:**

```rust
if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
    debug!("Failed to add discovered address to peer_store {:?} {:?}", err, addr);
}
```

The `EvictionFailed` error is only logged at `debug` level; no rate-limiting, no disconnect, no alert.

**Address validity — `network/src/protocols/discovery/mod.rs`, lines 332–341:**

`is_valid_addr` only rejects private/loopback IPs via `is_reachable`. Any public IP across 4096 distinct `/16` subnets passes. The attacker does not need to own or control these IPs.

**Message capacity:** `MAX_ADDR_TO_SEND = 1000` items × `MAX_ADDRS = 3` addresses = 3000 addresses per message. Filling 16384 slots requires approximately 6 crafted messages.

## Impact Explanation

Once the store is frozen, every subsequent `add_addr` call returns `Err(EvictionFailed)`. The node's peer discovery is permanently blocked: no new peer addresses can be recorded. If existing connections drop, the node cannot discover replacement peers and risks network isolation. The attack is applicable to any reachable CKB node and can be applied at scale across many nodes simultaneously with minimal per-node cost (one connection, ~6 messages), degrading the CKB network's peer discovery infrastructure broadly.

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — the attacker requires only one accepted connection and ~6 small messages to permanently degrade a node's ability to participate in peer discovery, with the effect maintainable indefinitely.

## Likelihood Explanation

- **Attacker capability:** Any peer that establishes one inbound or outbound P2P connection. No PoW, no key material, no privileged access required.
- **Required conditions:** The target node must accept at least one P2P connection (standard operation).
- **Feasibility:** ~6 crafted `Nodes` messages suffice to fill the store. The attacker does not need to own the advertised IPs.
- **Repeatability:** The attacker periodically re-advertises the same addresses to reset `attempts_count` via the `last_connected_at_ms >= existing` update path, preventing natural expiry.
- **Scalability:** A single attacker node can execute this against every peer it connects to.

## Recommendation

Change the strict inequality to `>=` at line 378 of `network/src/peer_store/peer_store_impl.rs`:

```rust
// Before
if addrs.len() > 4 {
// After
if addrs.len() >= 4 {
```

This ensures any group that has reached the per-subnet cap of 4 is eligible for eviction, closing the off-by-one gap. Additionally, consider rate-limiting `add_new_addrs` per session and adding a per-session cap on the number of addresses accepted from a single peer.

## Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Fill store: 4096 /16 subnets × 4 addresses = 16384 = ADDR_COUNT_LIMIT
for subnet in 0u16..4096 {
    for host in 1u8..=4 {
        let a = (subnet >> 8) as u8 + 1; // avoid 0.x.x.x
        let b = (subnet & 0xff) as u8;
        let addr: Multiaddr = format!(
            "/ip4/{}.{}.0.{}/tcp/8115/p2p/{}",
            a, b, host, PeerId::random().to_base58()
        ).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT; all 4096 groups have len == 4
// check_purge step 1: all entries connectable (last_connected_at_ms=0, attempts_count=0) → no eviction
// check_purge step 2: take(2048) groups, all len==4, none pass > 4 → candidate_peers empty → EvictionFailed
let new_addr: Multiaddr = format!(
    "/ip4/200.200.200.1/tcp/8115/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
// This assert fails — add_addr returns Err(EvictionFailed) instead of Ok(())
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_ok());
```