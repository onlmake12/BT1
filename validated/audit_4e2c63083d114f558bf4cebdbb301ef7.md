### Title
SUI Update Fees Permanently Locked in PriceInfoObject With No Withdrawal Path — (`target_chains/sui/contracts/sources/price_info.move`, `pyth.move`)

### Summary

All SUI fees paid via `update_single_price_feed` are unconditionally deposited into `PriceInfoObject` via `price_info::deposit_fee_coins`, but no on-chain function exists to withdraw or transfer those coins. The `fee_recipient_address` field in `State` is explicitly documented as no longer used. Fees accumulate permanently and are irrecoverable.

### Finding Description

In `pyth::update_single_price_feed`, the fee is deposited **before** the freshness check: [1](#0-0) 

`deposit_fee_coins` either creates a new `Coin<SUI>` dynamic object field on the `PriceInfoObject` or joins into the existing one: [2](#0-1) 

`price_info.move` exposes `get_balance` (read-only) and `deposit_fee_coins`, but **no** `withdraw_fees`, `transfer_fees`, or any function that moves coins out of the `PriceInfoObject`: [3](#0-2) 

The `State` struct holds a `fee_recipient_address` field: [4](#0-3) 

But `set_fee_recipient.move` explicitly documents that this field is vestigial: [5](#0-4) 

`fee_recipient_address` is never referenced in any transfer or coin-movement call anywhere in the Sui contract sources — it is only read by `get_fee_recipient` and written by `set_fee_recipient`. No code path ever routes coins from `PriceInfoObject` to this address.

The freshness check happens inside `update_cache`, which is called **after** the fee deposit: [6](#0-5) 

This means a caller submitting a stale price update still pays the fee, and that fee is permanently locked.

### Impact Explanation

Every SUI fee ever paid to `update_single_price_feed` — whether for a fresh or stale update — is permanently locked inside the corresponding `PriceInfoObject`. There is no governance action, admin function, or user-callable function that can recover these funds. Protocol revenue is entirely inaccessible, and users who pay fees for stale updates suffer an unrecoverable economic loss.

### Likelihood Explanation

This is not a theoretical edge case. Every single call to `update_single_price_feed` deposits fees into a permanent lock. The existing test at line 817 even asserts that fees accumulate in the object: [7](#0-6) 

No exploit is needed — the normal operation of the protocol causes this. An adversary can amplify the loss by submitting repeated stale updates (valid VAA, stale timestamp), each consuming a fee coin.

### Recommendation

1. Add a `withdraw_fee_coins` function (gated to `friend pyth::pyth` or a governance capability) in `price_info.move` that extracts the stored `Coin<SUI>` and transfers it to `fee_recipient_address`.
2. Either call this withdrawal inside `update_single_price_feed` (routing fees to the recipient immediately), or provide a separate privileged sweep function.
3. Consider reverting the transaction (or returning the fee coin) when `is_fresh_update` returns false, so users are not charged for no-op updates.

### Proof of Concept

1. Call `update_single_price_feed` with a valid VAA but a price timestamp ≤ the cached timestamp (stale update).
2. Observe that `price_info::get_balance(price_info_object)` increases by the fee amount.
3. Attempt to find any public or friend function in `price_info.move` that decreases this balance — none exists.
4. Confirm `fee_recipient_address` in `State` is never the target of a `transfer::public_transfer` or `coin::*` call anywhere in `target_chains/sui/contracts/sources/`.

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-277)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L316-322)
```text
        if (is_fresh_update(update, price_info_object)){
            pyth_event::emit_price_feed_update(price_feed::from(price_info::get_price_feed(update)), clock::timestamp_ms(clock)/1000);
            price_info::update_price_info_object(
                price_info_object,
                update
            );
        }
```

**File:** target_chains/sui/contracts/sources/pyth.move (L816-817)
```text
        // check fee coins are deposited in the price info object
        assert!(price_info::get_balance(&price_info_object_1)==DEFAULT_BASE_UPDATE_FEE, 0);
```

**File:** target_chains/sui/contracts/sources/price_info.move (L97-116)
```text
    public fun get_balance(price_info_object: &PriceInfoObject): u64 {
        if (!dynamic_object_field::exists_with_type<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY)) {
            return 0
        };
        let fee = dynamic_object_field::borrow<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY);
        coin::value(fee)
    }

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

**File:** target_chains/sui/contracts/sources/state.move (L43-54)
```text
    struct State has key, store {
        id: UID,
        governance_data_source: DataSource,
        stale_price_threshold: u64,
        base_update_fee: u64,
        fee_recipient_address: address,
        last_executed_governance_sequence: u64,
        consumed_vaas: ConsumedVAAs,

        // Upgrade capability.
        upgrade_cap: UpgradeCap
    }
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-2)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
```
