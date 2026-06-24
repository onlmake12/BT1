Audit Report

## Title
Unauthenticated Cache Bypass via `get_overview` Forcing Repeated Expensive System-Level Recomputation — (File: `rpc/src/module/terminal.rs`)

## Summary

The `get_overview` RPC method accepts a caller-controlled `refresh: Option<u32>` parameter with no authentication or rate limiting. When a caller passes `0x1F` (`RefreshKind::EVERYTHING`), the handler unconditionally clears all five in-process caches and re-executes every expensive sub-operation — including a full OS system scan, a `get_block_template` block assembly, a RocksDB key-count estimation, and a full peer enumeration — on every call. The Terminal module is enabled by default, and the RPC server has no authentication layer, making this trivially exploitable by any caller with access to the RPC port.

## Finding Description

The `get_overview` handler at `rpc/src/module/terminal.rs` lines 440–464 parses the raw `refresh` parameter and, when it equals `RefreshKind::EVERYTHING` (0x1F), immediately calls `self.cache.clear_all()` before re-fetching all data:

```rust
if refresh.contains(RefreshKind::EVERYTHING) {
    self.cache.clear_all();
}
```

`clear_all()` (lines 239–245) wipes all five `LruCache` entries simultaneously, invalidating cached data for all concurrent callers — not just the requester. After clearing, the handler unconditionally re-executes all sub-operations regardless of TTL:

1. **`get_sys_info`** (lines 509–510): Allocates `System::new_all()` and calls `sys.refresh_all()` — a blocking OS-level syscall enumerating all processes, memory, disks, and network interfaces.
2. **`get_tx_pool_info`** (lines 590–600): Calls `self.shared.get_block_template(None, None, None)` — a full tx-pool block assembly operation that contends directly with the miner's own block template pipeline.
3. **`get_cells_info`** (lines 636–643): Calls `estimate_num_keys_cf(COLUMN_CELL)` — a RocksDB column family key-count scan.
4. **`get_network_info`** (lines 664–693): Enumerates all connected peers.

The `clear_all()` call is redundant given that the per-subsystem refresh flags already bypass the TTL check in each sub-function, but its presence means the cache is wiped before re-population, creating a window where all concurrent legitimate callers are forced to re-fetch at full cost.

The RPC server (`rpc/src/server.rs`) has no authentication middleware. The `RpcConfig` struct (`util/app-config/src/configs/rpc.rs`) has no authentication fields. Rate limiting exists only for P2P protocols (relayer, hole-punching), not for HTTP RPC endpoints. The Terminal module is included in the default `modules` list in `resource/ckb.toml` line 190.

## Impact Explanation

An attacker with access to the RPC port can sustain a tight loop of `get_overview` calls with `refresh=31`, forcing repeated `get_block_template` invocations that contend with the miner's own block assembly pipeline. This constitutes a **High** impact: resource exhaustion that can easily crash or severely degrade a CKB node, and bad design that can cause CKB network congestion (delayed block production) with near-zero cost to the attacker. The `get_block_template` path is the most critical: it is the same code path used by the miner process, and saturating it with unauthenticated forced refreshes directly starves legitimate block template requests, potentially delaying block production.

## Likelihood Explanation

The attack requires only an HTTP POST to the default RPC port with a crafted JSON body — no credentials, no keys, no protocol knowledge beyond the public RPC documentation. The Terminal module is enabled by default. Many node operators, particularly miners requiring remote access, expose the RPC port beyond localhost despite the configuration warning. The attack is trivially scriptable with `curl`. A single attacker can sustain it indefinitely with no resource cost on their side.

## Recommendation

1. **Remove the `clear_all()` call** at line 447. The per-subsystem refresh flags in each sub-function already bypass the TTL check; `clear_all()` is redundant and only harms concurrent callers.
2. **Add a global rate limit** on forced refreshes (any non-zero `refresh` value) — e.g., at most one full refresh per TTL window per source IP or globally.
3. **Decouple `get_block_template` from `get_overview`**: the block template assembly should not be triggered by an unauthenticated cache-bypass path. Use `get_tx_pool_info` directly for pool statistics without assembling a full block template.
4. **Restrict the Terminal module** to authenticated callers, or document it as requiring firewall-level access control and disable it in the default configuration.

## Proof of Concept

```bash
# Attacker with access to the RPC port:
while true; do
  curl -s -X POST http://<node-rpc-host>:8114/ \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_overview","params":[31],"id":1}'
done
```

Each iteration triggers:
- `clear_all()` at line 447 — wipes all five caches for all concurrent callers
- `System::new_all()` + `sys.refresh_all()` at lines 509–510 — blocking OS enumeration
- `get_block_template(None, None, None)` at line 592 — full tx-pool block assembly contending with the miner pipeline
- `estimate_num_keys_cf(COLUMN_CELL)` at line 639 — RocksDB column scan

The node's CPU and I/O spike continuously. The miner's `get_block_template` pipeline is starved. No credential or privilege is required.