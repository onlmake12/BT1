### Title
Protocol Fee Revenue Permanently Locked in PriceInfoObjects — No Withdrawal Path Exists - (`target_chains/sui/contracts/sources/price_info.move`)

---

### Summary

All SUI fees collected via `update_single_price_feed` are deposited into each `PriceInfoObject`'s `FEE_STORAGE_KEY` dynamic field. No function anywhere in the production codebase extracts or transfers those coins out. The `fee_recipient_address` stored in `State` is never used to move coins. Every fee ever paid is permanently frozen.

---

### Finding Description

`update_single_price_feed` in `pyth.move` unconditionally calls `price_info::deposit_fee_coins` to store the caller's fee payment inside the target `PriceInfoObject`: [1](#0-0) 

`deposit_fee_coins` is declared `public` (no `friend`, no capability guard) and merges the incoming `Coin<SUI>` into the `FEE_STORAGE_KEY` dynamic object field: [2](#0-1) 

A full grep of every `.move` file under `target_chains/sui/contracts/sources/` for `withdraw`, `dynamic_object_field::remove`, `coin::split`, `coin::take`, or any transfer out of `FEE_STORAGE_KEY` returns **zero hits outside `price_info.move` itself**. The only operations on that field are `add`, `borrow`, and `borrow_mut` (for joining). There is no `remove`.

`State` carries a `fee_recipient_address` field and a governance action `SET_FEE_RECIPIENT`: [3](#0-2) 

But `set_fee_recipient.move` opens with the admission:

> "The previous version of the contract sent the fees to a recipient address but **this state is not used anymore**. This module is kept for backward compatibility." [4](#0-3) 

The governance action only updates the `fee_recipient_address` field in `State`; it never touches any `PriceInfoObject` or moves coins: [5](#0-4) 

Because `PriceInfoObject` is a shared object with `key + store` but no `drop`, and because Move's ownership model forbids destroying an object that still holds a live dynamic field, the locked `Coin<SUI>` cannot be recovered even through an upgrade unless a new entry point is explicitly added.

Additionally, because `deposit_fee_coins` is `public` with no access control, any unprivileged caller can push arbitrary `Coin<SUI>` into any `PriceInfoObject`, inflating the locked balance further (the attacker loses their own coins, but the coins are equally unrecoverable).

---

### Impact Explanation

Every SUI fee paid by every user through `update_single_price_feed` — the protocol's sole revenue mechanism on Sui — accumulates in per-feed shared objects from which it can never be extracted under the current bytecode. The `fee_recipient_address` governance knob is a dead letter. This constitutes **permanent protocol insolvency for fee revenue**: the protocol charges fees but can never collect them.

Scope match: *Permanent freezing of funds / protocol insolvency* — confirmed.

---

### Likelihood Explanation

This is not a future risk; it is the current, live behavior. Every call to `update_single_price_feed` already locks fees. No attacker action is required to trigger the core loss — ordinary users paying update fees are sufficient. The public `deposit_fee_coins` entry point makes the surface slightly larger but is not the primary cause.

---

### Recommendation

1. Add a `public(friend)` (or governance-gated) `withdraw_fee_coins` function in `price_info.move` that calls `dynamic_object_field::remove<vector<u8>, Coin<SUI>>` on `FEE_STORAGE_KEY` and returns the coin.
2. Add a corresponding entry point in `pyth.move` (guarded by `LatestOnly` and restricted to `fee_recipient_address`) that calls the above and transfers the coin via `sui::transfer::public_transfer`.
3. Remove the `public` visibility from `deposit_fee_coins` or add a capability guard so arbitrary callers cannot inflate locked balances.

---

### Proof of Concept

```
// Invariant test (pseudocode for Move test framework)
#[test]
fun test_fees_are_permanently_locked() {
    // 1. Setup Pyth state with base_update_fee = 50
    // 2. Create a PriceInfoObject via create_price_feeds
    // 3. Call update_single_price_feed with fee = coin::mint(50)
    // 4. Assert price_info::get_balance(&pio) == 50  // fee is in the object
    // 5. Attempt to call any function that returns Coin<SUI> from pio
    //    --> no such function exists; test cannot compile a withdrawal call
    // 6. Assert the 50 MIST is unrecoverable under current bytecode
}
```

The existing test already confirms step 4: [6](#0-5) 

No corresponding assertion for recovery exists anywhere in the test suite because no recovery path exists.

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-277)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L817-817)
```text
        assert!(price_info::get_balance(&price_info_object_1)==DEFAULT_BASE_UPDATE_FEE, 0);
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

**File:** target_chains/sui/contracts/sources/state.move (L48-49)
```text
        fee_recipient_address: address,
        last_executed_governance_sequence: u64,
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
