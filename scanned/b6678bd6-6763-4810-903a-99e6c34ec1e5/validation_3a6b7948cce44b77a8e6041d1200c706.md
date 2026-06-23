### Title
Missing Permission Check on `update_main_logger` / `set_extra_logger` RPC Methods Allows Any Caller to Suppress Node Logging or Exhaust Disk — (`File: rpc/src/module/debug.rs`)

---

### Summary

The CKB Debug RPC module exposes `update_main_logger` and `set_extra_logger` with no authentication or permission check of any kind. Any HTTP client that can reach the RPC port can invoke these methods to permanently silence all node logging or create unbounded log files on disk. The RPC server itself has no authentication middleware, and `DebugRpcImpl` carries no credential or caller-identity state. This is a direct structural analog to the `setBridge()` missing-permission-check class: a privileged state-mutating function callable by anyone without restriction.

---

### Finding Description

**Root cause — no permission check in the handler:**

`DebugRpcImpl` is a zero-field struct with no credential or session state:

```rust
#[derive(Clone)]
pub(crate) struct DebugRpcImpl {}
``` [1](#0-0) 

Both state-mutating methods execute unconditionally on any caller:

```rust
fn update_main_logger(&self, config: MainLoggerConfig) -> Result<()> {
    // no caller check, no token, no IP check
    Logger::update_main_logger(filter, to_stdout, to_file, color)...
}

fn set_extra_logger(&self, name: String, config_opt: Option<ExtraLoggerConfig>) -> Result<()> {
    if let Err(err) = Logger::check_extra_logger_name(&name) { ... } // only format check
    Logger::update_extra_logger(name, config.filter) ...
}
``` [2](#0-1) 

The only validation in `set_extra_logger` is a name-format check (alphanumeric + `-` + `_`), not a permission check: [3](#0-2) 

**Root cause — no authentication middleware in the RPC server:**

The HTTP server is built with `CorsLayer::permissive()` and no authentication layer. Every request is dispatched directly to `io.handle_call(call, T::default())`: [4](#0-3) 

`enable_debug()` mounts the module with no access-control wrapper: [5](#0-4) 

**Effect of `update_main_logger`:**

Sending `{"filter": "off", "to_stdout": false, "to_file": false}` causes the logger thread to set `log::set_max_level(Off)`, close the log file handle, and stop writing to stdout — permanently silencing all node logging until restart: [6](#0-5) 

**Effect of `set_extra_logger`:**

Each call with a new valid name causes the logger thread to call `open_log_file` which creates a new `.log` file in the node's log directory: [7](#0-6) 

```rust
fn open_log_file(file_path: &Path) -> Result<fs::File, String> {
    fs::OpenOptions::new().append(true).create(true).open(file_path)...
}
``` [8](#0-7) 

There is no cap on the number of extra loggers that can be registered.

---

### Impact Explanation

**Impact 1 — Unauthorized suppression of all node logging:**
An attacker calls `update_main_logger` with `filter: "off"`, `to_stdout: false`, `to_file: false`. All subsequent log records are silently dropped. The node operator loses all visibility into consensus events, peer bans, sync errors, and security-relevant warnings. This masks ongoing attacks and makes incident response impossible without a node restart.

**Impact 2 — Disk exhaustion via unbounded log file creation:**
An attacker calls `set_extra_logger` in a loop with distinct valid names (e.g., `a`, `b`, `aa`, `ab`, …). Each call creates a new `.log` file in the log directory and registers an open file descriptor. With no limit on the number of extra loggers, this exhausts disk inodes, disk space, or OS file-descriptor limits, causing the node process to malfunction or crash.

Both impacts are unauthorized state changes reachable with zero privilege.

---

### Likelihood Explanation

The Debug module is not in the default production `modules` list: [9](#0-8) 

However:
- It is explicitly listed as an available module and is commonly enabled by operators for diagnostics.
- Once enabled, there is no code-level barrier — no token, no IP allowlist, no signature. The protection is purely operational (firewall/binding), not enforced by the code itself.
- The RPC port is configurable to any address; operators who expose it publicly (a documented risk) are fully vulnerable.
- The attack requires only a single HTTP POST request with a valid JSON-RPC body — trivially automatable.

---

### Recommendation

1. **Add a caller-identity / token check** to all state-mutating Debug RPC methods. The RPC config already has a `listen_address` field; add an optional `rpc_secret_token` that must be supplied as a bearer token or HTTP header for any write operation.
2. **Cap the number of extra loggers** that can be registered (e.g., 16) to prevent disk exhaustion regardless of authentication.
3. **Separate the Debug module** into read-only (safe) and write (privileged) sub-methods, and gate the write methods behind an explicit permission check rather than relying solely on the module-enable config flag.

---

### Proof of Concept

**PoC 1 — Silence all logging (single HTTP request):**

```bash
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1,
    "jsonrpc": "2.0",
    "method": "update_main_logger",
    "params": [{"filter": "off", "to_stdout": false, "to_file": false}]
  }'
# Expected: {"jsonrpc":"2.0","result":null,"id":1}
# Effect: all subsequent CKB log output is permanently suppressed until restart
```

**PoC 2 — Disk exhaustion (loop):**

```bash
for i in $(seq 1 10000); do
  curl -s -X POST http://127.0.0.1:8114/ \
    -H 'Content-Type: application/json' \
    -d "{\"id\":$i,\"jsonrpc\":\"2.0\",\"method\":\"set_extra_logger\",\"params\":[\"logger$i\",{\"filter\":\"trace\"}]}"
done
# Effect: 10,000 .log files created in the node log directory,
#         10,000 open file descriptors held by the logger thread,
#         disk space and inode exhaustion
```

**Preconditions:** Debug module enabled in `rpc.modules` config (common in dev/staging; used by operators for diagnostics). No other privilege required.

### Citations

**File:** rpc/src/module/debug.rs (L38-39)
```rust
#[derive(Clone)]
pub(crate) struct DebugRpcImpl {}
```

**File:** rpc/src/module/debug.rs (L59-94)
```rust
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

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```

**File:** rpc/src/service_builder.rs (L192-196)
```rust
    /// Mounts methods from module Debug if it is enabled in the config.
    pub fn enable_debug(mut self) -> Self {
        let methods = DebugRpcImpl {};
        set_rpc_module_methods!(self, "Debug", debug_enable, add_debug_rpc_methods, methods)
    }
```

**File:** util/logger-service/src/lib.rs (L250-299)
```rust
                        Ok(Message::UpdateMainLogger {
                            filter,
                            to_stdout,
                            to_file,
                            color,
                        }) => {
                            if let Some(filter) = filter {
                                *filter_for_update.write() = filter;
                            }
                            if let Some(to_stdout) = to_stdout {
                                main_logger.to_stdout = to_stdout;
                            }
                            if let Some(to_file) = to_file {
                                main_logger.to_file = to_file;
                                if main_logger.to_file {
                                    if main_logger.file.is_none() {
                                        main_logger.file =
                                            Self::open_log_file(&main_logger.file_path).ok();
                                    }
                                } else {
                                    main_logger.file = None;
                                }
                            }
                            if let Some(color) = color {
                                main_logger.color = color;
                            }
                        }
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
                        Ok(Message::Terminate) | Err(_) => {
                            break;
                        }
                    }
                    let max_level = Self::max_level_filter(
                        &filter_for_update.read(),
                        &extra_loggers_for_update.read(),
                    );
                    log::set_max_level(max_level);
```

**File:** util/logger-service/src/lib.rs (L314-326)
```rust
    fn open_log_file(file_path: &Path) -> Result<fs::File, String> {
        fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(file_path)
            .map_err(|err| {
                format!(
                    "Cannot write to log file given: {:?} since {}",
                    file_path.as_os_str(),
                    err
                )
            })
    }
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
