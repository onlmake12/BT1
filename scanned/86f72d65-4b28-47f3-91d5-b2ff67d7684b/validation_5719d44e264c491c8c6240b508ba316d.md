Audit Report

## Title
Synchronous `System::new_all()` on Tokio Worker Thread via `get_overview{refresh:1}` Enables Local RPC DoS — (`rpc/src/module/terminal.rs`)

## Summary

`get_sys_info` calls `System::new_all()` and `sys.refresh_all()` synchronously on a tokio worker thread with no `spawn_blocking` wrapper. When `refresh=0x1` (SYSTEM_INFO bit) is passed, the 5-second TTL cache is unconditionally bypassed on every call. Sending N concurrent requests where N equals the tokio thread pool size pins all worker threads, stalling all other RPC operations for the duration of the blocking syscalls.

## Finding Description

The cache guard in `get_sys_info` is short-circuited when `refresh.contains(RefreshKind::SYSTEM_INFO)` is true: [1](#0-0) 

When bypassed, execution falls through to two blocking sysinfo calls with no `spawn_blocking` or `block_in_place` wrapper: [2](#0-1) 

`get_overview` is a synchronous `fn` (not `async fn`) called directly from the async `handle_jsonrpc` handler: [3](#0-2) [4](#0-3) 

The RPC runtime is sized to `available_parallelism()` (number of CPU cores): [5](#0-4) 

Sending N concurrent `get_overview` requests with `refresh=1` (where N ≥ thread pool size) blocks every tokio worker thread in the blocking sysinfo syscalls. The 30-second `TimeoutLayer` cannot fire because timer futures are polled by the same worker threads that are blocked: [6](#0-5) 

## Impact Explanation

All pending RPC calls — including `send_transaction`, `get_tip_header`, and `get_block_template` — queue indefinitely until a worker thread is freed. The RPC server becomes unresponsive for the duration of the attack. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash/unresponsiveness**. The attack is local-only (`127.0.0.1:8114`) and does not crash the node process itself, limiting severity to Note.

## Likelihood Explanation

The Terminal module is enabled in the default production config: [7](#0-6) 

The RPC listens on `127.0.0.1:8114` with no authentication: [8](#0-7) 

Any local process (unprivileged user, co-located container, malicious script) can trigger this. No rate limiting, per-client quota, or concurrency cap exists on this endpoint. The attacker needs only a loop sending one HTTP POST per iteration, with N parallel requests matching the CPU core count.

## Recommendation

1. Wrap the `System::new_all()` / `sys.refresh_all()` block in `tokio::task::spawn_blocking` so it executes on the dedicated blocking thread pool, not on a tokio worker thread.
2. Use `sysinfo::RefreshKind` to load only the specific subsystems needed (memory, current process) rather than `new_all()` which enumerates every process on the system.
3. Add a per-endpoint concurrency limit or rate limit to prevent cache-bypass abuse (e.g., one forced refresh per TTL window regardless of caller).

## Proof of Concept

```bash
# Pin all worker threads (adjust N to match CPU core count):
for i in $(seq 1 $(nproc)); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_overview","params":[1],"id":1}' &
done

# Concurrently measure latency of an unrelated call:
time curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_tip_header","params":[],"id":2}'
# Expected: response time >> normal; may hang until a worker thread is freed.
```

The call chain is confirmed by the code: `get_overview{refresh:1}` → `get_sys_info` (cache bypassed) → `System::new_all()` (blocking, no `spawn_blocking`) → `sys.refresh_all()` (blocking again) → worker thread stalled → concurrent RPC calls queue indefinitely. [9](#0-8)

### Citations

**File:** rpc/src/module/terminal.rs (L440-440)
```rust
    fn get_overview(&self, refresh: Option<u32>) -> Result<Overview> {
```

**File:** rpc/src/module/terminal.rs (L500-570)
```rust
    fn get_sys_info(&self, refresh: RefreshKind) -> Result<SysInfo> {
        // Check cache first unless force refresh
        if !refresh.contains(RefreshKind::SYSTEM_INFO)
            && let Some(cached) = self.cache.get_sys_info()
        {
            return Ok(cached);
        }

        // Fetch fresh system data
        let mut sys = System::new_all();
        sys.refresh_all();

        let total_memory = sys.total_memory();
        let used_memory = sys.used_memory();
        let global_cpu_usage = sys.global_cpu_usage();
        let sys_disks = SysDisks::new_with_refreshed_list();
        let disks = sys_disks
            .iter()
            .map(|disk| Disk {
                total_space: disk.total_space(),
                available_space: disk.available_space(),
                is_removable: disk.is_removable(),
            })
            .collect();
        let sys_networks = SysNetworks::new_with_refreshed_list();
        let networks = sys_networks
            .iter()
            .map(|(name, data)| Network {
                interface_name: name.clone(),
                received: data.received(),
                total_received: data.total_received(),
                transmitted: data.transmitted(),
                total_transmitted: data.total_transmitted(),
            })
            .collect();

        let global = Global {
            total_memory,
            used_memory,
            global_cpu_usage,
            disks,
            networks,
        };

        let process = sys
            .process(
                sysinfo::get_current_pid()
                    .map_err(|e| RPCError::custom(RPCError::CKBInternalError, e))?,
            )
            .ok_or_else(|| {
                RPCError::custom(RPCError::CKBInternalError, "failed to get current process")
            })?;

        let sys_disk_usage = process.disk_usage();
        let sys_info = SysInfo {
            global,
            cpu_usage: process.cpu_usage(),
            memory: process.memory(),
            disk_usage: DiskUsage {
                total_written_bytes: sys_disk_usage.total_written_bytes,
                written_bytes: sys_disk_usage.written_bytes,
                total_read_bytes: sys_disk_usage.total_read_bytes,
                read_bytes: sys_disk_usage.read_bytes,
            },
            virtual_memory: process.virtual_memory(),
        };

        // Cache the result
        self.cache.set_sys_info(sys_info.clone());
        Ok(sys_info)
    }
```

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
```

**File:** rpc/src/server.rs (L257-258)
```rust
            Request::Single(call) => {
                let result = io.handle_call(call, T::default()).await;
```

**File:** ckb-bin/src/subcommand/run.rs (L92-95)
```rust
fn calc_rpc_threads_num(args: &RunArgs) -> usize {
    let system_parallelism: usize = available_parallelism().unwrap().into();
    let default_num = usize::max(system_parallelism, 1);
    args.config.rpc.threads.unwrap_or(default_num)
```

**File:** resource/ckb.toml (L182-182)
```text
listen_address = "127.0.0.1:8114" # {{
```

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```
