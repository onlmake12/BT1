### Title
Missing Sender Validation in `OP_UPGRADE_CONTRACT` Handler Allows Any Address to Trigger an Authorized Contract Upgrade — (File: `target_chains/ton/contracts/contracts/Main.fc`)

---

### Summary

In the Pyth TON contract's `recv_internal` dispatcher, the `OP_UPGRADE_CONTRACT` branch invokes `execute_upgrade_contract(data)` without any check on `sender_address`. Once governance has authorized an upgrade by writing a non-zero `upgrade_code_hash` into contract storage, **any unprivileged address** can trigger the upgrade by sending `OP_UPGRADE_CONTRACT` with the matching code cell. This is a direct analog of the reported "missing sender validation" class: a privileged state-transition handler that omits a caller identity check.

---

### Finding Description

`recv_internal` in `Main.fc` loads `sender_address` from the message envelope at line 38, then dispatches on `op`. For every other sensitive branch (`OP_EXECUTE_GOVERNANCE_ACTION`, `OP_UPDATE_GUARDIAN_SET`) the called function internally validates the VAA emitter or governance source. The `OP_UPGRADE_CONTRACT` branch is the exception:

```
} elseif (op == OP_UPGRADE_CONTRACT) {
    execute_upgrade_contract(data);   // sender_address is never consulted
```

`execute_upgrade_contract` in `Pyth.fc` performs only a code-hash equality check:

```
() execute_upgrade_contract(cell new_code) impure {
    load_data();
    int hash_code = cell_hash(new_code);
    throw_unless(ERROR_INVALID_CODE_HASH, upgrade_code_hash == hash_code);
    set_code(new_code);
    set_c3(new_code.begin_parse().bless());
    upgrade_code_hash = 0;
    store_data();
    throw(0);
}
```

`upgrade_code_hash` is set by the governance action `AUTHORIZE_UPGRADE_CONTRACT` (via `execute_authorize_upgrade_contract`). Once that governance VAA is relayed and the hash is stored, the upgrade window is open to **any sender** who possesses the pre-image (the compiled code cell), which is public once the governance proposal is published.

The `sender_address` variable is in scope at the call site but is never passed to or checked inside `execute_upgrade_contract`.

---

### Impact Explanation

1. **Governance cancellation race**: If the Pyth team discovers a bug in the authorized code after the governance VAA is relayed but before the upgrade is executed, they cannot cancel the upgrade in time — an attacker can front-run the cancellation by immediately triggering `OP_UPGRADE_CONTRACT`.
2. **Forced upgrade timing**: An attacker can force the upgrade to execute at a moment of their choosing (e.g., during peak-traffic periods, immediately after a governance relay, or before the team has completed off-chain readiness checks), bypassing the intended operational window.
3. **Irreversibility**: `set_code` + `throw(0)` is irreversible within the same transaction; once triggered, the old code is gone and `upgrade_code_hash` is zeroed, preventing any retry with the old code.

The attacker cannot deploy *arbitrary* code — only the governance-authorized code — so this is not a full code-execution takeover. However, the ability to force an irreversible, time-sensitive state change without authorization is a meaningful protocol-level impact.

---

### Likelihood Explanation

The window exists whenever a governance upgrade VAA has been relayed to the TON contract and `upgrade_code_hash != 0`. The compiled code cell is derivable from the public governance proposal (the hash is published on-chain; the code itself is open-source). Any TON address can send the message with a minimal TON value. No special privilege, leaked key, or oracle manipulation is required.

---

### Recommendation

Add a sender check at the start of `execute_upgrade_contract`, or in the `OP_UPGRADE_CONTRACT` branch of `recv_internal`, restricting execution to a designated operator address stored in contract state (e.g., the governance contract address or a multisig):

```
} elseif (op == OP_UPGRADE_CONTRACT) {
    load_data();
    throw_unless(ERROR_INVALID_SENDER,
        equal_slices(sender_address, authorized_upgrade_executor));
    execute_upgrade_contract(data);
```

Alternatively, store the authorized executor address alongside `upgrade_code_hash` in the governance authorization step and clear it on execution.

---

### Proof of Concept

1. Governance relays a valid `AUTHORIZE_UPGRADE_CONTRACT` VAA to the TON contract via `OP_EXECUTE_GOVERNANCE_ACTION`. This sets `upgrade_code_hash = H` in storage.
2. The compiled new-code cell `C` (where `cell_hash(C) == H`) is publicly available from the open-source repository or the governance proposal.
3. An attacker constructs an internal message:
   - `op = 4` (`OP_UPGRADE_CONTRACT`)
   - `data = C` (the authorized code cell)
   - Sent from **any** TON address with a small TON value.
4. `execute_upgrade_contract` passes the hash check, calls `set_code(C)`, resets `upgrade_code_hash = 0`, and throws exit code 0 — the contract is now running the new code.
5. If the Pyth team subsequently discovers a flaw in `C` and attempts to cancel by sending a new governance VAA to zero out `upgrade_code_hash`, the upgrade has already been irreversibly applied.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ton/contracts/contracts/Main.fc (L38-62)
```text
    slice sender_address = cs~load_msg_addr();  ;; load sender address

    ;; * A 32-bit (big-endian) unsigned integer `op`, identifying the `operation` to be performed, or the `method` of the smart contract to be invoked.
    int op = in_msg_body~load_uint(32);
    cell data = in_msg_body~load_ref();
    slice data_slice = data.begin_parse();

    ;; * The remainder of the message body is specific for each supported value of `op`.
    if (op == OP_UPDATE_GUARDIAN_SET) {
        ;; @notice Updates the guardian set based on a Wormhole VAA
        ;; @param data_slice - Slice containing the VAA with guardian set update information
        update_guardian_set(data_slice);
    } elseif (op == OP_UPDATE_PRICE_FEEDS) {
        ;; @notice Updates price feeds with the latest price data
        ;; @param msg_value - Amount of TON sent with the message (used for fee calculation)
        ;; @param data_slice - Slice containing the price feed update data
        update_price_feeds(msg_value, data_slice);
    } elseif (op == OP_EXECUTE_GOVERNANCE_ACTION) {
        ;; @notice Executes a governance action based on a Wormhole VAA
        ;; @param data_slice - Slice containing the VAA with governance action information
        execute_governance_action(data_slice);
    } elseif (op == OP_UPGRADE_CONTRACT) {
        ;; @notice Upgrades the contract code
        ;; @param data - Cell containing the new contract code
        execute_upgrade_contract(data);
```

**File:** target_chains/ton/contracts/contracts/Pyth.fc (L506-526)
```text
() execute_upgrade_contract(cell new_code) impure {
    load_data();
    int hash_code = cell_hash(new_code);
    throw_unless(ERROR_INVALID_CODE_HASH, upgrade_code_hash == hash_code);

    ;; Set the new code
    set_code(new_code);

    ;; Set the code continuation to the new code
    set_c3(new_code.begin_parse().bless());

    ;; Reset the upgrade code hash
    upgrade_code_hash = 0;

    ;; Store the data to persist the reset above
    store_data();

    ;; Throw an exception to end the current execution
    ;; The contract will be restarted with the new code
    throw(0);
}
```

**File:** target_chains/ton/contracts/contracts/Pyth.fc (L528-531)
```text
() execute_authorize_upgrade_contract(slice payload) impure {
    int code_hash = payload~load_uint(256);
    upgrade_code_hash = code_hash;
}
```

**File:** target_chains/ton/contracts/contracts/common/storage.fc (L12-12)
```text
global int upgrade_code_hash; ;; 256-bit unsigned integer
```
