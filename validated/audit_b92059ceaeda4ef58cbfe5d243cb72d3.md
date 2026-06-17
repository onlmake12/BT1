The evidence is conclusive. Let me verify there's no withdraw path anywhere in the contract suite.

### Title
Protocol Update Fees Permanently Locked in `PriceInfoObject` — No Withdrawal Path Exists - (`target_chains/sui/contracts/sources/pyth.move`)

---

### Summary

Every call to `update_single_price_feed` deposits the caller's fee `Coin<SUI>` into the target `PriceInfoObject` via `price_info::deposit_fee_coins`. No function in the entire contract suite ever removes or transfers those coins out. The `fee_recipient_address` stored in `State` is set by governance but is never read in any coin-transfer context. All accumulated protocol fees are permanently irrecoverable.

---

### Finding Description

**Fee deposit path** — `pyth.move` `update_single_price_feed` (line 277):

```move
price_info::deposit_fee_coins(price_info_object, fee);
``` [1](#0-0) 

`deposit_fee_coins` in `price_info.move` stores the coin as a dynamic object field keyed by `FEE_STORAGE_KEY` on the `PriceInfoObject`'s `UID`, using `coin::join` to accumulate subsequent deposits: [2](#0-1) 

**No withdrawal path** — a grep for `withdraw`, `dynamic_object_field::remove`, `transfer.*fee`, and `fee.*transfer` across all production `.move` files returns zero matches. The only operations on `FEE_STORAGE_KEY` are `exists_with_type` (read), `add` (write), and `borrow_mut` (join) — never `remove`. [3](#0-2) 

**`fee_recipient_address` is vestigial** — `State` stores the field and `get_fee_recipient` exposes it, but it is never passed to any coin transfer: [4](#0-3) [5](#0-4) 

The governance module `set_fee_recipient.move` itself carries an explicit admission in its module-level comment:

> "The previous version of the contract sent the fees to a recipient address but **this state is not used anymore**. This module is kept for backward compatibility." [6](#0-5) 

The governance action still calls `state::set_fee_recipient` to update the address, but that address is never subsequently read for any transfer: [7](#0-6) 

---

### Impact Explanation

Every `update_single_price_feed` call permanently locks `≥ base_update_fee` MIST of SUI inside the corresponding `PriceInfoObject` shared object. Because `PriceInfoObject` is shared (not owned), no address can unilaterally extract its dynamic object fields. The balance grows monotonically and is irrecoverable under the current bytecode. The total locked value equals the sum of all fees ever paid across all price feeds since deployment.

---

### Likelihood Explanation

This is not a conditional or edge-case path. It triggers on **every single** `update_single_price_feed` call, which is the primary production entry point for price updates. The protocol is live and accumulating fees continuously. No attacker action is required — the locking is an unconditional consequence of normal protocol operation.

---

### Recommendation

Add a privileged `withdraw_fees` function to `price_info.move` that calls `dynamic_object_field::remove<vector<u8>, Coin<SUI>>` on `FEE_STORAGE_KEY` and transfers the extracted coin to `state::get_fee_recipient(pyth_state)`. Gate it behind `LatestOnly` and restrict callers to a governance or admin module. Alternatively, redirect fees at collection time in `update_single_price_feed` by transferring the coin directly to `state::get_fee_recipient(pyth_state)` instead of calling `deposit_fee_coins`.

---

### Proof of Concept

1. Deploy the contract with `base_update_fee = 50`.
2. Call `update_single_price_feed` N times across any price feed(s), each time supplying exactly 50 MIST.
3. After each call, assert `price_info::get_balance(pio) == 50 * call_count` — balance grows monotonically.
4. Attempt to find any public or friend function that decreases `get_balance` — none exists.
5. Confirm `state::get_fee_recipient` returns a valid address, but grep the entire module for any `transfer::public_transfer` or `coin::split`/`coin::take` that references `fee_recipient_address` — zero results.
6. Conclude: `50 * N` MIST is permanently locked per price feed, with no on-chain recovery path under the current package version.

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-277)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/price_info.move (L97-103)
```text
    public fun get_balance(price_info_object: &PriceInfoObject): u64 {
        if (!dynamic_object_field::exists_with_type<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY)) {
            return 0
        };
        let fee = dynamic_object_field::borrow<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY);
        coin::value(fee)
    }
```

**File:** target_chains/sui/contracts/sources/price_info.move (L105-116)
```text
    public fun deposit_fee_coins(price_info_object: &mut PriceInfoObject, fee_coins: Coin<SUI>) {
        if (!dynamic_object_field::exists_with_type<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY)) {
            dynamic_object_field::add(&mut price_info_object.id, FEE_STORAGE_KEY, fee_coins);
        }
        else {
            let current_fee = dynamic_object_field::borrow_mut<vector<u8>, Coin<SUI>>(
                &mut price_info_object.id,
                FEE_STORAGE_KEY
            );
            coin::join(current_fee, fee_coins);
        };
    }
```

**File:** target_chains/sui/contracts/sources/state.move (L48-48)
```text
        fee_recipient_address: address,
```

**File:** target_chains/sui/contracts/sources/state.move (L119-121)
```text
    public fun get_fee_recipient(s: &State): address {
        s.fee_recipient_address
    }
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-2)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L15-18)
```text
    public(friend) fun execute(latest_only: &LatestOnly, state: &mut State, payload: vector<u8>) {
        let PythFeeRecipient { recipient } = from_byte_vec(payload);
        state::set_fee_recipient(latest_only, state, recipient);
    }
```
