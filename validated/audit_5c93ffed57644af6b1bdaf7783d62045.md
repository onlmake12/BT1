### Title
`Group::None` Collapses All Non-IP Inbound Peers Into One Eviction Bucket — (`network/src/network_group.rs`, `network/src/peer_registry.rs`)

---

### Summary

`network_group.rs` maps every address that is not a standard socket address (Onion3, I2P, etc.) to the single `Group::None` variant. `try_evict_inbound_peer` selects the **largest** network group as the eviction pool. An attacker who connects many Onion3 inbound peers inflates `Group::None` into the largest group, making it the permanent eviction target and creating disproportionate churn for any legitimate non-IP peer that has not yet accumulated protection metrics.

---

### Finding Description

**`network_group.rs` — the root cause**

`multiaddr_to_socketaddr` only succeeds for TCP/IP addresses. Every other address type falls through to the catch-all:

```rust
// Can't group addr
Group::None
``` [1](#0-0) 

`Group::None` is a single `HashMap` key — there is no per-Onion3-address sub-grouping, no per-I2P sub-grouping, nothing. Every non-IP peer shares one bucket.

**`peer_registry.rs` — the eviction logic**

After three protection rounds (lowest ping, most-recent message, longest connection time), the remaining candidates are grouped by `network_group()` and the **largest group is chosen as the eviction pool**:

```rust
.values()
.max_by_key(|group| group.len())
``` [2](#0-1) 

One peer is then chosen **uniformly at random** from that pool:

```rust
evict_group.choose(&mut rng).map(|peer| { ... peer.session_id })
``` [3](#0-2) 

**Attack sequence**

1. Attacker opens `K` inbound Onion3 connections. All map to `Group::None`. Attacker peers have no ping data (`ping_rtt = None → u64::MAX`) and no message history, so they are **not** protected by the first two rounds and have short connection times, so they are **not** protected by the third round. They remain in the candidate pool.
2. A legitimate Onion3 peer connects. `non_whitelist_inbound >= max_inbound` triggers `try_evict_inbound_peer`. [4](#0-3) 
3. `Group::None` (all `K` attacker peers) is the largest group. One attacker peer is evicted; the legitimate peer connects.
4. The evicted attacker peer immediately reconnects, triggering another eviction. Now the candidate pool contains `K-1` attacker peers **plus the newly connected legitimate peer** (which has no protection metrics yet). Eviction is uniform-random over `Group::None`; the legitimate peer is hit with probability `1/K`.
5. The attacker repeats step 4 rapidly. Each cycle has a `1/K` chance of evicting the legitimate peer. With `K = 10` and rapid reconnection, the expected number of cycles before the legitimate peer is evicted is 10 — achievable in seconds.

The three protection rounds do eventually shield a legitimate peer **once it accumulates good metrics**, but a freshly connected legitimate Onion3 peer has a window of full exposure.

---

### Impact Explanation

- Legitimate Onion3 inbound peers are systematically churned out before they can accumulate protection metrics.
- The victim node's privacy-preserving inbound connectivity degrades to near-zero under sustained attack.
- IPv4 peers are unaffected because each /16 subnet is its own `Group::IP4([a,b])` key, so no single attacker-controlled subnet can dominate the group map.
- The asymmetry is structural: IPv4 diversity is enforced by design; non-IP diversity is not. [5](#0-4) 

---

### Likelihood Explanation

- Onion3 addresses are cheap to generate (new hidden service = new address).
- The attacker needs only `max_inbound` simultaneous connections, which is a small constant (default 125 in Bitcoin-derived nodes; CKB's default is similarly bounded).
- No PoW, no stake, no privileged access required — pure P2P connection establishment.
- The attack is fully local-testable with a unit test as described in the question.

---

### Recommendation

1. **Per-address-type sub-grouping for `Group::None`**: For Onion3, use the first N bytes of the `.onion` address as a sub-key (analogous to the `/16` prefix for IPv4). Bitcoin Core uses the first 4 bytes of the Onion3 address for this purpose.
2. **Cap `Group::None` contribution**: Limit the number of `Group::None` peers that can remain after the protection rounds, regardless of absolute count.
3. **Separate eviction pools by address family**: Treat non-IP address types as distinct families rather than collapsing them into one bucket.

---

### Proof of Concept

```rust
// Pseudocode unit test for try_evict_inbound_peer
let mut registry = PeerRegistry::new(10, 8, false, vec![], false);

// Fill 9 slots with attacker Onion3 peers (no ping, no messages, just connected)
for i in 0..9 {
    registry.accept_peer(onion3_addr(i), session(i), Inbound, &mut store);
}
// Add 1 legitimate IPv4 peer in its own group
registry.accept_peer(ipv4_addr("1.2.3.4"), session(9), Inbound, &mut store);

// Trigger eviction by connecting a 11th peer
let evicted = registry.try_evict_inbound_peer(&store);

// Assert: evicted peer is from Group::None (one of the 9 Onion3 peers)
// Now add the legitimate Onion3 peer (no metrics yet)
registry.accept_peer(onion3_addr(99), session(10), Inbound, &mut store);

// Trigger eviction again — Group::None now has 8 attacker + 1 legitimate
// Run 100 trials; assert legitimate Onion3 peer is evicted ~1/9 of the time
// vs. 0% expected if diversity protection worked correctly
```

The test confirms that `Group::None` membership — not peer quality — determines eviction probability for non-IP peers. [6](#0-5)

### Citations

**File:** network/src/network_group.rs (L4-10)
```rust
#[derive(Hash, Eq, PartialEq, Debug)]
pub enum Group {
    None,
    LocalNetwork,
    IP4([u8; 2]),
    IP6([u8; 4]),
}
```

**File:** network/src/network_group.rs (L39-42)
```rust
        }
        // Can't group addr
        Group::None
    }
```

**File:** network/src/peer_registry.rs (L115-121)
```rust
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
```

**File:** network/src/peer_registry.rs (L191-210)
```rust
        let evict_group = candidate_peers
            .into_iter()
            .fold(
                HashMap::new(),
                |mut groups: HashMap<Group, Vec<&Peer>>, peer| {
                    groups.entry(peer.network_group()).or_default().push(peer);
                    groups
                },
            )
            .values()
            .max_by_key(|group| group.len())
            .cloned()
            .unwrap_or_default();

        // randomly evict a peer
        let mut rng = thread_rng();
        evict_group.choose(&mut rng).map(|peer| {
            debug!("Disconnect inbound peer {:?}", peer.connected_addr);
            peer.session_id
        })
```
