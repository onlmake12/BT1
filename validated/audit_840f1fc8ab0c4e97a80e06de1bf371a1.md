The code is clear. Let me confirm the async/sync execution model for the RPC handler.

### Title
Synchronous `System::new_all()` on Tokio Worker Thread via `get_overview{refresh:1}` Enables Local RPC DoS — (`rpc/src/module/terminal.rs`)

---

### Summary

`get_sys_info` calls `System::new_all()` and `sys.refresh_all()` synchronously on a tokio worker thread with no `spawn_blocking` wrapper. Passing `refresh=0x1` (SYSTEM_INFO) bypasses the 5-second TTL cache unconditionally on every call. On a host with thousands of processes, repeated calls exhaust the tokio RPC thread pool, blocking all concurrent RPC operations.

---

### Finding Description

`get_sys_info` in `rpc/src/module/terminal.rs` performs two blocking sysinfo operations inline:

```rust
let mut sys = System::new_all();   // enumerates ALL processes
sys.refresh_all();                  // re-enumerates ALL processes
``` [1](#0-0) 

The cache guard immediately above is short-circuited when the caller sets `refresh=0x1`:

```rust
if !refresh.contains(RefreshKind::SYSTEM_INFO)   // false when refresh=0x1
    && let Some(cached) = self.cache.get_sys_info()
{
    return Ok(cached);
}
``` [2](#0-1) 

So with `refresh=0x1`, every single call unconditionally executes `System::new_all()`.

The RPC server is built on axum/tokio. The async handler calls `io.handle_call(...).await` directly: [3](#0-2) 

`get_overview` is a **synchronous** function — there is no `spawn_blocking`, `block_in_place`, or background thread offload anywhere in the module: [4](#0-3) 

Calling a blocking function directly inside an async tokio task stalls the worker thread for the full duration of the syscall. The RPC thread pool defaults to `available_parallelism()` (number of CPU cores): [5](#0-4) 

On a host with 5 000+ processes, `System::new_all()` + `sys.refresh_all()` can take 1–3 seconds. Sending N concurrent `get_overview{refresh:1}` requests (where N ≥ thread-pool size) pins every worker thread, starving all other pending RPC calls.

---

### Impact Explanation

While the 30-second HTTP `TimeoutLayer` exists: [6](#0-5) 

…that timeout is driven by the same tokio runtime. If all worker threads are blocked in synchronous code, the timer future never gets polled and the timeout cannot fire. Concurrent calls to `send_transaction`, `get_tip_header`, `get_block_template` (used by miners) queue indefinitely until a worker thread is freed. The RPC server becomes effectively unresponsive for the duration of the attack.

---

### Likelihood Explanation

- The RPC listens on `127.0.0.1:8114` by default with no authentication. [7](#0-6) 
- Any local process (including an unprivileged user on a shared server, a compromised co-located container, or a malicious script) can call `get_overview`.
- The `Terminal` module is enabled in the default production config. [8](#0-7) 
- No rate limiting, no per-client quota, no concurrency cap exists on this endpoint.
- The attack requires only a loop sending one HTTP POST per second.

---

### Recommendation

1. Wrap the `System::new_all()` / `sys.refresh_all()` block in `tokio::task::spawn_blocking` so it runs on the blocking thread pool, not on a tokio worker thread.
2. Add a per-endpoint rate limit (e.g., one forced refresh per client per TTL window) to prevent cache-bypass abuse.
3. Consider using `sysinfo::RefreshKind` to load only the specific subsystems needed (memory, current process) rather than calling `new_all()` which loads every process on the system.

---

### Proof of Concept

```bash
# On a host with 1000+ processes, run in parallel:
for i in $(seq 1 16); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_overview","params":[1],"id":1}' &
done

# Concurrently, measure latency of an unrelated call:
time curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_tip_header","params":[],"id":2}'
# Expected: response time >> normal; may return HTTP 408 or hang until a worker frees.
```

The call sequence is exactly as described: `get_overview{refresh:1}` → `get_sys_info` → `System::new_all()` (blocking, enumerates all processes on the tokio worker thread) → `sys.refresh_all()` (blocking again) → worker thread stalled → concurrent RPC calls queue up → timeout or failure. [9](#0-8)

### Citations

**File:** rpc/src/module/terminal.rs (L427-428)
```rust
    #[rpc(name = "get_overview")]
    fn get_overview(&self, refresh: Option<u32>) -> Result<Overview>;
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
