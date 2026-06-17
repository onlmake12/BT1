### Title
Unvalidated User-Controlled `target_address` Allows Arbitrary Message Dispatch from Pyth TON Contract - (File: target_chains/ton/contracts/contracts/Main.fc / Pyth.fc)

### Summary
The `OP_PARSE_PRICE_FEED_UPDATES` and `OP_PARSE_UNIQUE_PRICE_FEED_UPDATES` handlers in the Pyth TON contract accept a `target_address` directly from user-supplied message body with no validation. The contract then unconditionally sends a response message — carrying all remaining TON balance and attacker-controlled `custom_payload` — to that arbitrary address. This allows any unprivileged sender to weaponize the Pyth contract as a message relay to any TON address, with the message appearing to originate from the trusted Pyth contract.

### Finding Description

In `Main.fc`, both `OP_PARSE_PRICE_FEED_UPDATES` and `OP_PARSE_UNIQUE_PRICE_FEED_UPDATES` load `target_address` directly from the caller-controlled message body:

```
slice target_address = in_msg_body~load_msg_addr();   // line 77 / line 95
```

This value is passed without any check into `parse_price_feed_updates` / `parse_unique_price_feed_updates`, which call `send_price_feeds_response`. That function sends a raw TON message to `target_address`:

```
send_raw_message(begin_cell()
    .store_uint(0x18, 6)
    .store_slice(target_address)          ;; attacker-controlled destination
    .store_coins(0)
    .store_uint(1, MSG_SERIALIZE_BITS)
    .store_ref(response.end_cell())
    .end_cell(),
    SENDMODE::CARRY_ALL_BALANCE);         ;; sends all remaining balance
```

The `response` cell contains:
- The op-code (`OP_PARSE_PRICE_FEED_UPDATES` / `OP_PARSE_UNIQUE_PRICE_FEED_UPDATES`)
- Verified price feed data
- The actual `sender_address` (from the real message sender)
- The attacker-controlled `custom_payload`

There is no check that `target_address == sender_address`, no whitelist of approved receiver contracts, and no restriction on `custom_payload` content. The code itself acknowledges the downstream risk in a comment:

```
;; SECURITY: Integrators MUST validate that messages are from this Pyth contract
;; in their receive function. Otherwise, attackers could:
;; 1. Send invalid price responses
;; 2. Impersonate users via sender_address and custom_payload fields
;; 3. Potentially drain the protocol
```

However, the Pyth contract itself performs no such validation before dispatching.

### Impact Explanation

1. **Arbitrary message injection**: Any unprivileged actor can cause the Pyth contract to send a message to any TON address. The message appears to originate from the Pyth contract address, which is a trusted oracle. Victim contracts that check `msg.sender == pyth_address` but do not further validate the embedded `sender_address` or `custom_payload` can be manipulated.

2. **Attacker-controlled payload delivery**: `custom_payload` is entirely attacker-supplied and is forwarded verbatim to the victim. A victim contract that parses `custom_payload` (e.g., to determine a recipient, amount, or action) can be tricked into executing unintended logic.

3. **TON balance forwarding**: `SENDMODE::CARRY_ALL_BALANCE` forwards all remaining balance after the fee reserve to `target_address`. An attacker can redirect excess TON to an address of their choosing rather than back to the actual sender, breaking the expected refund flow.

4. **Spam / DoS of arbitrary contracts**: Any TON contract can be flooded with well-formed Pyth-signed messages at the cost of the update fee, potentially disrupting contracts that have limited message-handling capacity or that trigger expensive logic on receipt.

### Likelihood Explanation

Likelihood is **medium-high**. The entry path requires only a valid Wormhole-attested price update (freely obtainable from Hermes) and a fee payment. No privileged role is needed. The attacker simply sets `target_address` to any desired victim. The attack is cheap, repeatable, and requires no special knowledge beyond the TON message schema documented in the SDK.

### Recommendation

1. **Validate `target_address` against `sender_address`**: Require `target_address` to equal the actual message sender unless an explicit whitelist is maintained.
2. **Whitelist approved receiver contracts**: Maintain an admin-controlled set of approved `target_address` values, analogous to the `is_valid_data_source` dictionary already used for data source validation.
3. **Restrict or hash `custom_payload`**: Do not forward raw attacker-controlled bytes to third-party contracts. At minimum, document and enforce a maximum length.
4. **Use `sender_address` as the response destination by default**: If the design intent is that the caller receives the response, default `target_address` to `sender_address` and require explicit opt-in for redirection.

### Proof of Concept

1. Attacker obtains a valid BTC/USD price update from Hermes (public API).
2. Attacker constructs a TON internal message to the Pyth contract with:
   - `op = OP_PARSE_PRICE_FEED_UPDATES`
   - Valid `update_data` (Hermes VAA)
   - `price_ids` = [BTC_PRICE_FEED_ID]
   - `min_publish_time` / `max_publish_time` = current timestamp ± 60s
   - `target_address` = address of victim DeFi contract on TON
   - `custom_payload` = crafted bytes (e.g., encoding a large withdrawal amount)
3. Attacker sends the message with sufficient TON to cover the update fee.
4. The Pyth contract verifies the price data (legitimate), then calls `send_price_feeds_response`, which dispatches a message to the victim contract carrying the attacker's `custom_payload` and the Pyth contract as the apparent sender.
5. If the victim contract trusts messages from the Pyth address and acts on `custom_payload`, it executes attacker-intended logic.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ton/contracts/contracts/Main.fc (L73-80)
```text
        cell price_ids_cell = in_msg_body~load_ref();
        slice price_ids_slice = price_ids_cell.begin_parse();
        int min_publish_time = in_msg_body~load_uint(64);
        int max_publish_time = in_msg_body~load_uint(64);
        slice target_address = in_msg_body~load_msg_addr();
        cell custom_payload_cell = in_msg_body~load_ref();
        slice custom_payload = custom_payload_cell.begin_parse();
        parse_price_feed_updates(msg_value, data_slice, price_ids_slice, min_publish_time, max_publish_time, sender_address, target_address, custom_payload);
```

**File:** target_chains/ton/contracts/contracts/Main.fc (L91-98)
```text
        cell price_ids_cell = in_msg_body~load_ref();
        slice price_ids_slice = price_ids_cell.begin_parse();
        int publish_time = in_msg_body~load_uint(64);
        int max_staleness = in_msg_body~load_uint(64);
        slice target_address = in_msg_body~load_msg_addr();
        cell custom_payload_cell = in_msg_body~load_ref();
        slice custom_payload = custom_payload_cell.begin_parse();
        parse_unique_price_feed_updates(msg_value, data_slice, price_ids_slice, publish_time, max_staleness, sender_address, target_address, custom_payload);
```

**File:** target_chains/ton/contracts/contracts/Pyth.fc (L332-366)
```text
() send_price_feeds_response(tuple price_feeds, int msg_value, int op, slice sender_address, slice target_address, slice custom_payload) impure {
    ;; Build response cell with price feeds
    builder response = begin_cell()
        .store_uint(op, 32)  ;; Response op
        .store_uint(price_feeds.tlen(), 8);  ;; Number of price feeds

    ;; Create and store price feed cell chain
    cell price_feeds_cell = create_price_feed_cell_chain(price_feeds);
    cell custom_payload_cell = begin_cell().store_slice(custom_payload).end_cell();
    response = response.store_ref(price_feeds_cell).store_slice(sender_address).store_ref(custom_payload_cell);

    ;; Calculate update fee
    int update_fee = single_update_fee * price_feeds.tlen();

    ;; Reserve current_balance + fee
    raw_reserve(update_fee, RESERVE_MODE::INCREASE_BY_ORIGINAL_BALANCE);

    ;; SECURITY: Integrators MUST validate that messages are from this Pyth contract
    ;; in their receive function. Otherwise, attackers could:
    ;; 1. Send invalid price responses
    ;; 2. Impersonate users via sender_address and custom_payload fields
    ;; 3. Potentially drain the protocol
    ;;
    ;; Note: This message is bounceable. If the target contract rejects the message,
    ;; the excess TON will remain in this contract and won't be automatically refunded to the
    ;; original sender. Integrators should handle failed requests and refunds in their implementation.
    send_raw_message(begin_cell()
        .store_uint(0x18, 6)
        .store_slice(target_address)
        .store_coins(0)
        .store_uint(1, MSG_SERIALIZE_BITS)
        .store_ref(response.end_cell())
        .end_cell(),
        SENDMODE::CARRY_ALL_BALANCE);
}
```

**File:** target_chains/ton/contracts/contracts/Pyth.fc (L407-422)
```text
() parse_price_feed_updates(int msg_value, slice update_data_slice, slice price_ids_slice, int min_publish_time, int max_publish_time, slice sender_address, slice target_address, slice custom_payload) impure {
    try {
        load_data();

        ;; Use the helper function to parse price IDs
        tuple price_ids = parse_price_ids_from_slice(price_ids_slice);

        tuple price_feeds = parse_price_feeds_from_data(msg_value, update_data_slice, price_ids, min_publish_time, max_publish_time, false);
        send_price_feeds_response(price_feeds, msg_value, OP_PARSE_PRICE_FEED_UPDATES,
            sender_address, target_address, custom_payload);
    } catch (_, error_code) {
        ;; Handle any unexpected errors
        emit_error(error_code, OP_PARSE_PRICE_FEED_UPDATES,
            sender_address, begin_cell().store_slice(custom_payload).end_cell());
    }
}
```

**File:** target_chains/ton/contracts/contracts/Pyth.fc (L424-438)
```text
() parse_unique_price_feed_updates(int msg_value, slice update_data_slice, slice price_ids_slice, int publish_time, int max_staleness, slice sender_address, slice target_address, slice custom_payload) impure {
    try {
        load_data();

        ;; Use the helper function to parse price IDs
        tuple price_ids = parse_price_ids_from_slice(price_ids_slice);

        tuple price_feeds = parse_price_feeds_from_data(msg_value, update_data_slice, price_ids, publish_time, publish_time + max_staleness, true);
        send_price_feeds_response(price_feeds, msg_value, OP_PARSE_UNIQUE_PRICE_FEED_UPDATES, sender_address, target_address, custom_payload);
    } catch (_, error_code) {
        ;; Handle any unexpected errors
        emit_error(error_code, OP_PARSE_UNIQUE_PRICE_FEED_UPDATES,
            sender_address, begin_cell().store_slice(custom_payload).end_cell());
    }
}
```
