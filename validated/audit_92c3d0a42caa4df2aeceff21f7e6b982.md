Audit Report

## Title
`check_purge` `take(len/2)` Integer Truncation Causes O(N) Amortized `add_addr` Cost Under 2-Group Adversarial Fill — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
When the peer store reaches `ADDR_COUNT_LIMIT` (16384) and all stored addresses belong to exactly 2 network groups with no non-connectable entries, `check_purge`'s pass-2 `take(len / 2)` evaluates to `take(1)` due to integer truncation, evicting only 2 peers per invocation. Because each invocation performs two full O(N) scans over all 16384 entries and reduces the store by only 2, the purge re-triggers every 2 insertions, degrading the amortized cost of `add_addr` from O(1) to O(N).

## Finding Description
`add_addr` unconditionally calls `check_purge` before inserting: [1](#0-0) 

`check_purge` returns early only if `count() < ADDR_COUNT_LIMIT`: [2](#0-1) 

**Pass 1** iterates all entries via `addrs_iter()` collecting non-connectable peers. Addresses added via `add_addr` are created with `last_connected_at_ms=0` and `attempts_count=0`: [3](#0-2) 

`is_connectable` returns `true` for these because the non-connectable conditions require `attempts_count >= ADDR_MAX_RETRIES (3)` or `>= ADDR_MAX_FAILURES (10)`, neither of which is met: [4](#0-3) 

So pass 1 collects nothing and the `if candidate_peers.is_empty()` branch is entered.

**Pass 2** iterates all entries again, groups by network segment using the first two IPv4 octets: [5](#0-4) 

Then computes `len = peers_by_network_group.len()` and applies `take(len / 2)`: [6](#0-5) 

With exactly 2 groups (e.g., all addresses in `10.0.x.x` and `172.16.x.x`), `len = 2` and `take(2 / 2) = take(1)`. Only the largest group is visited; since 8192 > 4, exactly 2 peers are evicted. The store drops from 16384 → 16382. The new address is inserted → 16383. The next `add_addr` call finds 16383 < 16384 and skips purge; the address is inserted → 16384. The call after that triggers purge again. The cycle repeats every 2 insertions, with each purge performing two full O(16384) iterations.

## Impact Explanation
This is a sustained CPU overhead attack on a single CKB node. An attacker keeping the peer store in the 2-group steady state forces approximately 32768 hash-map iterations per 2 peer advertisements processed. This constitutes a meaningful and repeatable performance degradation exploitable at negligible cost, fitting **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
The P2P discovery protocol calls `add_addr` for each advertised peer address with no authentication or proof-of-work. A single attacker-controlled peer can advertise arbitrary addresses. Two /16 IPv4 subnets (e.g., `10.0.0.0/16` and `172.16.0.0/16`) provide 65536 × 2 = 131072 unique addresses — sufficient to sustain the attack indefinitely. No privilege escalation or victim mistake is required; the attacker only needs one inbound or outbound connection to the victim node.

## Recommendation
Replace `take(len / 2)` with logic that guarantees enough evictions to bring the store below `ADDR_COUNT_LIMIT`. At minimum, compute the number of evictions needed and iterate over enough groups to satisfy it:

```rust
let needed = self.addr_manager.count() - ADDR_COUNT_LIMIT + 1;
let mut collected = 0;
let candidate_peers: Vec<_> = peers
    .into_iter()
    .take_while(|_| collected < needed)
    .flat_map(|addrs| {
        if addrs.len() > 4 {
            let chosen = addrs.iter()
                .choose_multiple(&mut rand::thread_rng(), 2)
                .into_iter()
                .map(|a| a.addr.clone())
                .collect::<Vec<_>>();
            collected += chosen.len();
            Some(chosen)
        } else { None }
    })
    .flatten()
    .collect();
```

Alternatively, remove the `take` entirely and evict from all groups until the store is below the limit.

## Proof of Concept
Fill the peer store with 8192 addresses in `10.0.x.x` and 8192 in `172.16.x.x` (each unique IP:port with a unique peer ID to avoid deduplication via `base_addr`). Confirm `addr_manager.count() == 16384`. Then call `add_addr` repeatedly with new addresses and assert that the store oscillates between 16382 and 16384, confirming only 2 evictions per purge cycle. Each purge invocation performs two full O(16384) scans (pass 1 via `addrs_iter()` at line 341, pass 2 via `addrs_iter()` at line 360), confirming O(N) amortized cost per insertion. [7](#0-6)

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

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L340-403)
```rust
        let now_ms = ckb_systemtime::unix_time_as_millis();
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

        if candidate_peers.is_empty() {
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
        }
        Ok(())
```

**File:** network/src/peer_store/types.rs (L65-76)
```rust
    pub fn new(addr: Multiaddr, last_connected_at_ms: u64, score: Score, flags: u64) -> Self {
        AddrInfo {
            // only store tcp protocol
            addr: base_addr(&addr),
            score,
            last_connected_at_ms,
            last_tried_at_ms: 0,
            attempts_count: 0,
            random_id_pos: 0,
            flags,
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```
