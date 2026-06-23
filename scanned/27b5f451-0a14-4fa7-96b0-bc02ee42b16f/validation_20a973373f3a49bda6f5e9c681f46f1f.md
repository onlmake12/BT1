### Title
Peer-Controlled Disconnect Message Logged Without Sanitization Enables Log Injection — (File: `network/src/protocols/disconnect_message.rs`)

### Summary

The `DisconnectMessageProtocol` handler accepts an arbitrary UTF-8 string from any remote P2P peer and writes it directly into the node's log at `info!` level with no length limit, no newline stripping, and no content sanitization. This is the CKB analog to the XSS class: user-controlled string data stored/displayed without sanitization. Any unprivileged peer can inject fake log lines — including forged timestamps, forged log levels, and forged source targets — into the node operator's log files and stdout.

### Finding Description

**Root cause** — `network/src/protocols/disconnect_message.rs`, `received()`:

```rust
async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
    let session_id = context.session.id;
    if let Ok(message) = String::from_utf8(data.to_vec()) {
        info!(
            "Received disconnect message from peer={}: {}",
            session_id, message
        );
    }
    ...
}
``` [1](#0-0) 

The only gate is `String::from_utf8` — valid UTF-8 passes unconditionally. There is no maximum length check, no newline/control-character stripping, and no escaping before the string is interpolated into the `info!` macro. The `info!` macro formats the final log line and writes it to stdout and the log file via the logger service. [2](#0-1) 

The logger's `sanitize_color` function only strips ANSI color escape sequences; it does not strip newlines or other control characters. [3](#0-2) 

**Attack path:**

1. Attacker establishes a TCP connection to any CKB node's P2P port (default 8115) — no authentication required.
2. Attacker opens the `DisconnectMessage` protocol (protocol ID registered in `SupportProtocols::DisconnectMessage`).
3. Attacker sends a crafted payload such as:

```
legitimate disconnect\n2026-06-20 12:00:00.000 +00:00 LogWriter ERROR ckb_chain  CRITICAL: chain fork detected, attacker block accepted
```

4. The node logs the entire multi-line payload at `info!` level, injecting the fake `ERROR` line into the log file and stdout. [4](#0-3) 

The code comment at line 33 even acknowledges the gap: `"Maybe punish this peer later (also when send us too large message)"` — confirming the length and content issues are known but unaddressed. [5](#0-4) 

### Impact Explanation

An unprivileged remote peer can inject arbitrary content into the CKB node's log files and stdout stream. Concrete impacts:

- **Forged security events**: Inject fake `ERROR`/`WARN` lines mimicking consensus failures, chain forks, or key compromise alerts, causing operators to take incorrect remediation actions (e.g., emergency shutdown, rollback).
- **Log analysis tool poisoning**: SIEM/alerting systems parsing structured log output can be fed false data, triggering false alarms or suppressing real ones.
- **Audit trail corruption**: Injected lines with crafted timestamps can make forensic analysis of a real incident unreliable.
- **Unbounded log growth**: No length limit means a peer can send a single message of arbitrary size (e.g., hundreds of MB), causing disk exhaustion on the node.

Severity: **Medium** — matches the original report's severity. No direct asset theft, but integrity of the operator's observability and decision-making is compromised, and disk exhaustion is a reachable DoS vector.

### Likelihood Explanation

- **No privilege required**: Any TCP-reachable peer can open the `DisconnectMessage` protocol. The P2P port is publicly exposed by design.
- **Trivially exploitable**: Sending a crafted UTF-8 string over the protocol requires only a basic P2P client implementation.
- **No existing mitigations**: The only check is UTF-8 validity. No rate limiting, no length cap, no content filtering exists in the handler.
- **Realistic attacker**: A malicious peer or a researcher probing the network can trigger this with a single connection.

### Recommendation

1. **Strip or escape newlines and control characters** from the peer-supplied message before logging:
   ```rust
   let sanitized = message.replace(['\n', '\r', '\x00'..='\x1f'], " ");
   info!("Received disconnect message from peer={}: {}", session_id, sanitized);
   ```
2. **Enforce a maximum message length** (e.g., 256 bytes) and disconnect/ban peers that exceed it, consistent with the existing comment's intent.
3. **Log at `debug!` level** instead of `info!` to reduce the visibility and impact of injected content in production deployments.

### Proof of Concept

**Steps:**

1. Connect to a CKB node's P2P port.
2. Negotiate the `DisconnectMessage` protocol (protocol ID `0x4`).
3. Send the following UTF-8 payload (no framing beyond the Tentacle length-delimited codec):

```
normal disconnect\n2026-06-20 00:00:00.000 +00:00 LogWriter ERROR ckb_chain  [INJECTED] Consensus failure: invalid block accepted at height 99999999
```

4. Observe the node's log file or stdout. The injected `ERROR` line appears as a genuine log entry indistinguishable from real node output.

**Unbounded size PoC:** Send a 100 MB all-`A` UTF-8 payload. The node logs it in full, writing 100 MB to the log file in a single operation, exhausting disk space if repeated.

### Citations

**File:** network/src/protocols/disconnect_message.rs (L12-38)
```rust
// A protocol for just receive string message and log it use debug level.
pub(crate) struct DisconnectMessageProtocol(Arc<NetworkState>);

impl DisconnectMessageProtocol {
    pub(crate) fn new(network_state: Arc<NetworkState>) -> Self {
        DisconnectMessageProtocol(network_state)
    }
}

#[async_trait]
impl ServiceProtocol for DisconnectMessageProtocol {
    async fn init(&mut self, _context: &mut ProtocolContext) {}

    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        let session_id = context.session.id;
        if let Ok(message) = String::from_utf8(data.to_vec()) {
            info!(
                "Received disconnect message from peer={}: {}",
                session_id, message
            );
        } else {
            // Maybe punish this peer later (also when send us too large message)
            debug!(
                "[WARNING]: peer {} send us a malformed disconnect message!",
                session_id
            );
        }
```

**File:** util/logger-service/src/lib.rs (L227-240)
```rust
                                if main_logger.to_stdout {
                                    let output = if main_logger.color {
                                        data.as_str()
                                    } else {
                                        removed_color.as_str()
                                    };
                                    println!("{output}");
                                }
                                if main_logger.to_file
                                    && let Some(mut file) = main_logger.file.as_ref()
                                {
                                    let _ = file.write_all(removed_color.as_bytes());
                                    let _ = file.write_all(b"\n");
                                };
```

**File:** util/logger-service/src/lib.rs (L473-476)
```rust
fn sanitize_color(s: &str) -> String {
    let re = RE.get_or_init(|| Regex::new("\x1b\\[[^m]+m").expect("Regex compile success"));
    re.replace_all(s, "").to_string()
}
```
