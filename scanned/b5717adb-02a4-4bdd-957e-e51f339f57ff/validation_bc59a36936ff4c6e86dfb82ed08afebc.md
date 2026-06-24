Audit Report

## Title
Peer Store Permanently Fillable via Discovery Flood with Exactly-4-per-Group Addresses, Causing Permanent `EvictionFailed` — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` applies a strictly-greater-than-4 threshold (`addrs.len() > 4`) when selecting eviction candidates by network group. An unprivileged remote peer can flood the discovery protocol with exactly 16384 addresses spread across 4096 `/16` groups at exactly 4 addresses each. Under this layout every group fails the threshold, `candidate_peers` is empty, and `check_purge` returns `Err(PeerStoreError::EvictionFailed)` on every subsequent `add_addr` call. Honest peer addresses are then silently dropped, permanently disabling peer discovery for the victim node.

## Finding Description

**Entry point — unauthenticated discovery:**

`DiscoveryAddressManager::add_new_addrs` is called from the `received` handler for `DiscoveryMessage::Nodes` with no authentication or privilege requirement. [1](#0-0) 

Errors from `peer_store.add_addr` are silently discarded at `debug!` level — no disconnect, no penalty. [2](#0-1) 

**`add_addr` calls `check_purge` before inserting:**

`check_purge` is invoked unconditionally before any insertion; if it returns `Err`, the address is dropped. [3](#0-2) 

**`check_purge` — the flawed eviction logic:**

`check_purge` only runs eviction logic when `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384). [4](#0-3) 

Step 1 collects non-connectable addresses. Fresh addresses have `last_connected_at_ms=0` and `attempts_count=0`, so neither the `>= ADDR_MAX_RETRIES` (3) nor `>= ADDR_MAX_FAILURES` (10) condition is met — `is_connectable` returns `true` for all of them, yielding an empty `candidate_peers`. [5](#0-4) 

Step 2 groups by network segment, sorts by group size descending, takes the top `len/2` groups, and applies the critical threshold: [6](#0-5) 

The condition `if addrs.len() > 4` at line 378 is **strictly greater than 4**. With 4096 groups × 4 addresses each, every group has exactly 4 members, so the condition is false for all groups, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. [7](#0-6) 

**`ADDR_COUNT_LIMIT` is 16384:** [8](#0-7) 

**Message-size limits do not prevent the attack:**

`MAX_ADDR_TO_SEND=1000` and `MAX_ADDRS=3` per node item means ~3000 addresses per `Nodes` message; filling 16384 slots requires only ~6 messages from a single or a few colluding peers. [9](#0-8) 

## Impact Explanation

Once the peer store is filled with the attacker's 4-per-group layout, the victim node cannot add any new peer addresses via the discovery protocol. This permanently degrades the node's ability to find honest peers, making it susceptible to eclipse attacks. An eclipsed CKB node can be fed a false chain view, leading to consensus deviation for that node — matching the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"* and potentially *"Vulnerabilities which could easily cause consensus deviation"* for the isolated node.

## Likelihood Explanation

The attack requires only:
- One or a few connections to the victim node (no special privileges)
- IP diversity across 4096 `/16` blocks — the attacker does not need to own those IPs, only advertise them via discovery `Nodes` messages
- Approximately 6 `Nodes` messages to fill the store

Fresh addresses remain "connectable" (`attempts_count=0`) until the victim actually dials and fails them. The node does not aggressively dial all 16384 addresses immediately, so the attack window can persist for hours to days. The attacker can sustain the attack by continuously sending new diverse addresses to replace any that age out.

## Recommendation

Change the eviction threshold from strictly greater than (`> 4`) to greater than or equal to (`>= 4`) at line 378 of `network/src/peer_store/peer_store_impl.rs`, so that groups of exactly 4 are also eviction candidates:

```rust
// Before:
if addrs.len() > 4 {
// After:
if addrs.len() >= 4 {
```

Additionally, consider applying a misbehavior penalty or disconnecting a session when `EvictionFailed` is encountered repeatedly from the same session, rather than silently discarding the error in `add_new_addrs`.

## Proof of Concept

```rust
// Unit test: fill peer store with 4096 groups × 4 addresses = 16384 entries,
// then assert add_addr returns Err(EvictionFailed).
let mut peer_store = PeerStore::default();
for group in 0u32..4096 {
    for host in 1u32..=4 {
        let ip = std::net::Ipv4Addr::new(
            ((group >> 8) & 0xff) as u8,
            (group & 0xff) as u8,
            0,
            host as u8,
        );
        let addr: Multiaddr = format!("/ip4/{}/tcp/8115", ip).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with exactly 4 per /16 group
let new_addr: Multiaddr = "/ip4/200.200.200.1/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L347-362)
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

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L374-392)
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
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
