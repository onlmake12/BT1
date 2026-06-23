### Title
Peer Store Monopolization via Diverse-Subnet Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

An unprivileged remote peer can fill the peer store to `ADDR_COUNT_LIMIT` (16 384) by cycling short-lived connections and advertising up to 10 unique addresses per identify message, each from a distinct /16 network group. Once full, `check_purge` enters the network-group eviction path but finds no group with >4 peers, returns `EvictionFailed`, and every subsequent `add_addr` call for a legitimate peer silently drops the address. The peer store is permanently monopolized by attacker-controlled entries until the node manually cleans them up.

---

### Finding Description

**Entrypoint — `IdentifyCallback::add_remote_listen_addrs`**

On every completed identify handshake, the remote peer's advertised listen addresses are unconditionally forwarded to `peer_store.add_addr`: [1](#0-0) 

`add_addr` calls `check_purge()` before inserting: [2](#0-1) 

**Network-group granularity — `/16`, not `/24`**

`Group` is keyed on the first **two** octets of the IPv4 address: [3](#0-2) 

So the attacker needs addresses from distinct /16 subnets (65 536 available in IPv4) — far easier than /24 diversity.

**`check_purge` eviction logic**

Step 1 removes non-connectable addresses. A freshly injected address has `last_connected_at_ms = 0` and `attempts_count = 0`, which passes `is_connectable`: [4](#0-3) 

So step 1 removes nothing. Step 2 groups addresses by network group, sorts by group size descending, takes the top half, and evicts 2 random addresses **only from groups with >4 peers**: [5](#0-4) 

If every group has exactly 1 address (one per /16 subnet), the `addrs.len() > 4` guard is never satisfied, `candidate_peers` is empty, and the function returns: [6](#0-5) 

**Silent drop of legitimate addresses**

The caller in `add_remote_listen_addrs` only logs the error: [7](#0-6) 

The legitimate address is silently discarded.

**`ADDR_COUNT_LIMIT` = 16 384** [8](#0-7) 

With `MAX_ADDRS = 10` addresses per identify message, the attacker needs ≈ 1 638 completed identify handshakes to fill the store.

---

### Impact Explanation

- The peer store is monopolized by attacker-controlled addresses from diverse /16 subnets.
- All subsequent `add_addr` calls for legitimate peers return `EvictionFailed` and are silently dropped.
- `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` return only attacker-controlled addresses, wasting outbound connection slots on non-listening IPs.
- Legitimate peer discovery is starved; the node cannot learn about new honest peers.
- This is a prerequisite for an eclipse attack: once the peer store is monopolized, the attacker can selectively allow some addresses to become "connectable" to steer the victim's outbound connections.

---

### Likelihood Explanation

- The attacker needs only one real IP to cycle connections; each connection contributes 10 addresses from arbitrary advertised IPs (not the connection source IP).
- No PoW, no key material, no privileged role required — only the ability to complete P2P identify handshakes.
- The attack is self-sustaining: as the node's feeler logic eventually marks injected addresses as non-connectable (after `ADDR_MAX_RETRIES = 3` failures), the attacker reconnects and re-injects fresh addresses faster than the node can clean them up.
- The attack is local-testable with a single machine.

---

### Recommendation

1. **Per-source-IP address quota**: Limit the number of addresses that can be contributed by a single session or source IP within a time window.
2. **Lower the eviction threshold**: Replace `addrs.len() > 4` with `addrs.len() >= 1` (or ≥ 2) so that even singleton groups are eligible for eviction when the store is full.
3. **Prefer connected/verified addresses during eviction**: Prioritize evicting addresses with `last_connected_at_ms == 0` (never successfully connected) over verified addresses.
4. **Cap unverified addresses**: Maintain a separate, smaller quota for addresses received via identify (unverified) vs. addresses from successful outbound connections (verified).

---

### Proof of Concept

```
1. Fill peer store:
   for i in 0..1638:
       connect to victim node
       complete identify handshake
       send listen_addrs = [
           /ip4/{i*10+0}.{i}.0.1/tcp/8115/p2p/<peer_id_0>,
           /ip4/{i*10+1}.{i}.0.1/tcp/8115/p2p/<peer_id_1>,
           ... (10 addresses, each from a distinct /16 subnet)
       ]
       disconnect

2. Assert peer store is full (count == 16384).

3. Attempt to add a legitimate address:
   connect as honest peer
   send identify with listen_addrs = [/ip4/1.2.3.4/tcp/8115/p2p/<legit_id>]

4. Assert: peer store still contains 16384 attacker addresses;
   legit address is absent; check_purge returned EvictionFailed.
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
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

**File:** network/src/peer_store/peer_store_impl.rs (L358-401)
```rust
            let candidate_peers: Vec<_> = {
                let mut peers_by_network_group: HashMap<Group, Vec<_>> = HashMap::default();
                for addr in self.addr_manager.addrs_iter() {
                    peers_by_network_group
                        .entry((&addr.addr).into())
                        .or_default()
                        .push(addr);
                }
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
