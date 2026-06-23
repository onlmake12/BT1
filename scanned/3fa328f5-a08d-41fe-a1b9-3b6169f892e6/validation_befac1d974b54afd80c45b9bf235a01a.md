### Title
Unsanitized Peer-Controlled String Injected into Log Output and RPC Subscription Channel — (`File: network/src/protocols/disconnect_message.rs`)

---

### Summary

Any unprivileged P2P peer can send an arbitrary UTF-8 string as a `DisconnectMessage` protocol payload. The CKB node logs this string verbatim via `info!()` with no sanitization, no length limit, and no control-character stripping. The same raw string is forwarded as `original_message` through the `NotifyController` into the RPC subscription log stream, where external subscribers receive it unmodified.

---

### Finding Description

In `network/src/protocols/disconnect_message.rs`, the `received` handler accepts any valid UTF-8 payload from a peer and logs it directly:

```rust
// network/src/protocols/disconnect_message.rs L25-38
async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
    let session_id = context.session.id;
    if let Ok(message) = String::from_utf8(data.to_vec()) {
        info!(
            "Received disconnect message from peer={}: {}",
            session_id, message   // ← attacker-controlled, unsanitized
        );
    } else {
        // Maybe punish this peer later (also when send us too large message)
        debug!("[WARNING]: peer {} send us a malformed disconnect message!", session_id);
    }
``` [1](#0-0) 

The only gate is `String::from_utf8` — any valid UTF-8 string passes. The inline comment explicitly acknowledges that size limits are **not** enforced ("Maybe punish this peer later (also when send us too large message)").

The `info!()` macro routes the record through the logger service. In `util/logger-service/src/lib.rs`, the `Message::Record` handler sends `original_message` (the raw, unsanitized peer string) to the `NotifyController`:

```rust
// util/logger-service/src/lib.rs L219-225
if let Some(notifier) = &notifier {
    notifier.notify_log(LogEntry {
        level,
        message: original_message,   // ← raw peer content
        date,
        target,
    });
}
``` [2](#0-1) 

Note that `sanitize_color` (ANSI stripping) is applied only to the file/stdout rendering path, **not** to `original_message` before it is forwarded to the notifier. [3](#0-2) 

The `NotifyController` delivers these `LogEntry` values to the RPC subscription module, which broadcasts them to all WebSocket subscribers:

```rust
// rpc/src/module/subscription.rs L308-309
Some(log_entry) = log_receiver.recv() => {
    publiser_send!(ckb_jsonrpc_types::LogEntry, convert_log_entry(log_entry), log_sender);
},
``` [4](#0-3) 

---

### Impact Explanation

Three concrete injection surfaces exist:

1. **Log file / stdout injection**: A peer embeds `\n` (or `\r\n`) characters in the disconnect message. The logger writes the multi-line string as a single `write_all` call, producing fake log lines that appear structurally identical to genuine CKB log entries. An operator or automated log-analysis tool cannot distinguish injected lines from real ones.

2. **ANSI escape-code injection in terminal output**: When `main_logger.color = true`, the raw `data` string (which includes the peer content) is printed to stdout without ANSI stripping. A peer can embed terminal control sequences (cursor movement, color resets, overwrite sequences) to visually corrupt or hide real log output in an operator's terminal.

3. **RPC subscription channel injection**: Any client subscribed to the CKB log subscription RPC receives `LogEntry { message: <peer-controlled string>, level: Info, ... }`. Monitoring dashboards, alerting pipelines, or wallet backends that consume this stream receive attacker-controlled content indistinguishable from genuine node log messages.

---

### Likelihood Explanation

The attacker only needs to establish a TCP connection to the CKB P2P port (default 8115) and open the `DisconnectMessage` sub-protocol (protocol ID is publicly documented in `SupportProtocols`). No authentication, no stake, no special role is required. The attack is trivially repeatable from any internet-reachable host. [5](#0-4) 

---

### Recommendation

1. **Sanitize before logging**: Strip or escape control characters (`\n`, `\r`, `\x1b`, etc.) from the peer-supplied string before passing it to `info!()`. A simple approach is to replace any byte `< 0x20` (except printable ASCII) with a placeholder such as `·` or a hex escape.

2. **Enforce a length limit**: Reject (and optionally ban) peers that send disconnect messages exceeding a reasonable bound (e.g., 256 bytes). The existing comment acknowledges this is missing.

3. **Sanitize `original_message` before notifier dispatch**: The `original_message` field forwarded to `NotifyController` should be sanitized independently of the file/stdout path, since RPC subscribers consume it directly.

---

### Proof of Concept

Connect to a CKB node's P2P port, negotiate the `DisconnectMessage` sub-protocol, and send the following UTF-8 payload:

```
\n2024-01-01 00:00:00.000 INFO  ckb_chain::chain  block 99999999 verified
```

The CKB node will write two lines to its log file:

```
2024-01-01 12:34:56.789 INFO  ckb_network  Received disconnect message from peer=42: 
2024-01-01 00:00:00.000 INFO  ckb_chain::chain  block 99999999 verified
```

The second line is entirely attacker-fabricated but is structurally indistinguishable from a genuine chain log entry. Any RPC subscriber to the log channel simultaneously receives a `LogEntry` with `message` set to the injected string.

### Citations

**File:** network/src/protocols/disconnect_message.rs (L25-38)
```rust
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

**File:** util/logger-service/src/lib.rs (L210-217)
```rust
                            let removed_color = if (is_match
                                && (!main_logger.color || main_logger.to_file))
                                || !extras.is_empty()
                            {
                                sanitize_color(data.as_ref())
                            } else {
                                "".to_owned()
                            };
```

**File:** util/logger-service/src/lib.rs (L219-225)
```rust
                                if let Some(notifier) = &notifier {
                                    notifier.notify_log(LogEntry {
                                        level,
                                        message: original_message,
                                        date,
                                        target,
                                    });
```

**File:** rpc/src/module/subscription.rs (L308-309)
```rust
                        Some(log_entry) = log_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::LogEntry, convert_log_entry(log_entry), log_sender);
```

**File:** network/src/protocols/support_protocols.rs (L1-10)
```rust
use crate::ProtocolId;
use p2p::{
    builder::MetaBuilder,
    service::{ProtocolHandle, ProtocolMeta},
    traits::ServiceProtocol,
};
use tokio_util::codec::length_delimited;

pub const LASTEST_VERSION: &str = "3";

```
