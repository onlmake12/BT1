### Title
Peer Store Permanently Locked by Adversarial Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

The `check_purge` second-phase eviction logic contains an off-by-one condition (`addrs.len() > 4`) that produces an empty eviction candidate list when all /16 network groups contain exactly 4 addresses. An unprivileged remote peer can exploit this via the discovery protocol to permanently prevent any new address from being stored, starving the node of outbound connection candidates.

---

### Finding Description

The vulnerability is in `check_purge` in `peer_store_impl.rs`. The function is called by `add_addr` every time a new discovered address is submitted. When `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384), it runs two eviction phases:

**Phase 1** collects addresses where `!is_connectable(now_ms)`: [1](#0-0) 

**Phase 2** (entered only when phase 1 finds nothing) groups by `/16` network group, sorts by group size descending, takes the top `len/2` groups, and evicts 2 from each group — **but only if `addrs.len() > 4`**: [2](#0-1) 

The `Group` type for IPv4 is `IP4([bits[0], bits[1]])` — the first two octets, i.e., the /16 subnet: [3](#0-2) 

**The exploit:** An attacker sends 16384 unique IPv4 addresses distributed across exactly 4096 distinct /16 subnets, with exactly 4 addresses per subnet. All addresses are submitted via the discovery `Nodes` message → `add_new_addrs` → `add_addr`: [4](#0-3) 

`add_addr` always stores addresses with `last_connected_at_ms=0` and `attempts_count=0`: [5](#0-4) 

With those values, `is_connectable` returns `true` (neither the `>= ADDR_MAX_RETRIES` nor `>= ADDR_MAX_FAILURES` threshold is met): [6](#0-5) 

So phase 1 finds zero candidates. In phase 2, every group has `len == 4`, and `4 > 4` is `false`, so `flat_map` yields nothing. `candidate_peers` is empty, and the function returns: [7](#0-6) 

Every subsequent `add_addr` call hits the same dead end. The error is silently swallowed at the debug log level: [8](#0-7) 

---

### Impact Explanation

Once the store is locked, the node cannot record any new peer addresses. `fetch_addrs_to_attempt` and `fetch_addrs_to_feeler` can only draw from the 16384 attacker-controlled addresses, none of which are reachable (the attacker controls them and keeps them unresponsive). The node exhausts its outbound connection budget on dead addresses, becomes unable to discover honest peers, and loses sync with the honest chain tip — causing consensus isolation.

---

### Likelihood Explanation

The attack requires only P2P connectivity. The discovery protocol accepts `Nodes` messages from any connected peer with up to `MAX_ADDR_TO_SEND=1000` addresses per message: [9](#0-8) 

Filling 16384 slots requires ~17 rounds of `Nodes` messages (or fewer peers relaying more rounds). The attacker needs globally routable IPv4 addresses (to pass `is_valid_addr`), which is trivially achievable using real but attacker-controlled IP space or by spoofing addresses in the /16 groups. The condition is deterministic and reproducible.

---

### Recommendation

Change the eviction threshold from strictly-greater-than to greater-than-or-equal-to-2 (or simply `>= 1`), so any non-empty group is eligible for eviction:

```rust
// current (vulnerable):
if addrs.len() > 4 {

// fixed:
if addrs.len() >= 2 {
```

Additionally, consider evicting from all groups (not just the top `len/2`) when the store is full and no invalid addresses exist, and add a fallback that always evicts at least one entry (e.g., the oldest `last_tried_at_ms`) to guarantee `check_purge` never returns `EvictionFailed` when the store is full of connectable addresses.

---

### Proof of Concept

```rust
#[test]
fn test_adversarial_eviction_lockout() {
    let mut peer_store = PeerStore::default();
    // Fill with 16384 addresses: 4096 /16 subnets × 4 addresses each
    let mut count = 0u32;
    'outer: for a in 1u8..=255 {
        for b in 0u8..=255 {
            for port in 1u16..=4 {
                if count >= 16384 { break 'outer; }
                let addr: Multiaddr = format!(
                    "/ip4/{}.{}.1.1/tcp/{}/p2p/{}",
                    a, b, port, PeerId::random().to_base58()
                ).parse().unwrap();
                peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
                count += 1;
            }
        }
    }
    assert_eq!(peer_store.addr_manager().count(), 16384);

    // Now any new add_addr must fail with EvictionFailed
    let new_addr: Multiaddr = format!(
        "/ip4/10.0.0.1/tcp/9999/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
    assert!(result.is_err()); // EvictionFailed — store permanently locked
    assert_eq!(peer_store.addr_manager().count(), 16384);
}
```

### Citations

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

**File:** network/src/peer_store/peer_store_impl.rs (L341-355)
```rust
        let candidate_peers: Vec<_> = self
            .addr_manager
            .addrs_iter()
            .filter_map(|addr| {
                if !addr.is_connectable(now_ms) {
                    Some(addr.addr.clone())
                } else {
                    None
                }
            })
            .collect();

        for key in candidate_peers.iter() {
            self.addr_manager.remove(key);
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L374-401)
```rust
                peers
                    .into_iter()
                    .take(len / 2)
                    .flat_map(move |addrs| {
                        if addrs.len() > 4 {
                            Some(
                                addrs
                                    .iter()
                                    .choose_multiple(&mut rand::thread_rng(), 2)
                                    .into_iter()
                                    .map(|addr| addr.addr.clone())
                                    .collect::<Vec<Multiaddr>>(),
                            )
                        } else {
                            None
                        }
                    })
                    .flatten()
                    .collect()
            };

            for key in candidate_peers.iter() {
                self.addr_manager.remove(key);
            }

            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/protocols/discovery/mod.rs (L279-288)
```rust
    } else if nodes.items.len() > MAX_ADDR_TO_SEND {
        warn!(
            "Too many items (announce=false) length={}",
            nodes.items.len()
        );
        misbehavior = Some(Misbehavior::TooManyItems {
            announce: nodes.announce,
            length: nodes.items.len(),
        });
    }
```

**File:** network/src/protocols/discovery/mod.rs (L347-363)
```rust
    fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
        if addrs.is_empty() {
            return;
        }

        for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
            trace!("Add discovered address:{:?}", addr);
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
        }
    }
```

**File:** network/src/peer_store/types.rs (L89-105)
```rust
    pub fn is_connectable(&self, now_ms: u64) -> bool {
        // do not remove addr tried in last minute
        if self.tried_in_last_minute(now_ms) {
            return true;
        }
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
        // consider addr is not connectable if failed too many times
        if now_ms.saturating_sub(self.last_connected_at_ms) > ADDR_TIMEOUT_MS
            && (self.attempts_count >= ADDR_MAX_FAILURES)
        {
            return false;
        }
        true
    }
```
