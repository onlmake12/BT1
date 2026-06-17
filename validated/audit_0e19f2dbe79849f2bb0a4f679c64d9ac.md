### Title
Fee Permanently Locked in PriceInfoObject on Stale Update — No Refund Path — (`target_chains/sui/contracts/sources/pyth.move`)

---

### Summary

In `update_single_price_feed`, the fee is unconditionally deposited into the `PriceInfoObject` **before** the freshness check executes. If `is_fresh_update` returns `false`, the price is silently skipped but the fee is already gone. No refund path exists anywhere in the contract. Fees accumulate in `PriceInfoObject` indefinitely with no withdrawal mechanism.

---

### Finding Description

The execution order in `update_single_price_feed` is:

1. Assert fee ≥ base fee [1](#0-0) 
2. **Deposit fee into `PriceInfoObject`** [2](#0-1) 
3. Search for matching `price_identifier` in the hot-potato vector [3](#0-2) 
4. Call `update_cache`, which internally calls `is_fresh_update` and **silently skips the write** if the update is not newer [4](#0-3) 

`deposit_fee_coins` merges the caller's `Coin<SUI>` into a dynamic object field on the `PriceInfoObject`:

```move
coin::join(current_fee, fee_coins);
``` [5](#0-4) 

There is no `withdraw_fee_coins`, no `refund`, and no conditional deposit anywhere in the Sui Pyth contract tree. The only fee-related functions on `PriceInfoObject` are `deposit_fee_coins` (public) and `get_balance` (read-only). [6](#0-5) 

The `set_fee_recipient` governance module explicitly states: *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore."* — confirming there is no active fee-drain path from `PriceInfoObject` to any recipient. [7](#0-6) 

---

### Impact Explanation

Any caller of `update_single_price_feed` who submits a valid but stale accumulator message loses their `Coin<SUI>` fee permanently. The coins are locked inside the `PriceInfoObject`'s dynamic field with no on-chain mechanism to recover them — not by the payer, not by governance, not by any fee recipient. This is a direct, permanent loss of user funds matching the stated scope.

---

### Likelihood Explanation

This is trivially reachable by any unprivileged user:

- Submit the same accumulator message twice in two separate transactions. The second call passes all validation (valid Wormhole VAA, valid data source, valid Merkle proof), deposits the fee, then silently skips the price write because `is_fresh_update` returns `false`.
- Any race condition where two users submit the same update in the same epoch produces the same outcome for the slower submitter.
- No privileged access, no leaked keys, no governance majority required.

---

### Recommendation

Move the fee deposit **after** the freshness check, or refund the fee if `is_fresh_update` returns `false`. Concretely, restructure `update_single_price_feed` so that `deposit_fee_coins` is only called when `update_cache` actually writes a new price. Alternatively, abort the transaction when the update is stale (forcing the caller to use a fresh message), which also prevents the fee loss.

---

### Proof of Concept

```
1. Deploy Pyth on Sui localnet with base_update_fee = F.
2. Obtain a valid accumulator message M for price_identifier X at timestamp T.
3. Call create_authenticated_price_infos_using_accumulator(M) → hot_potato_1.
4. Call update_single_price_feed(hot_potato_1, price_info_object_X, coin_F_1) → price updated to T, fee F locked in price_info_object_X.
5. Call create_authenticated_price_infos_using_accumulator(M) again → hot_potato_2.  (VAA replay is allowed here because wormhole VAA replay protection is per-VAA hash and the accumulator path calls vaa::take_payload consuming the VAA, but the accumulator message itself can be re-submitted.)
6. Call update_single_price_feed(hot_potato_2, price_info_object_X, coin_F_2).
   - deposit_fee_coins executes: balance of price_info_object_X = 2F.
   - is_fresh_update returns false (T == T, not strictly greater).
   - Price not updated.
7. Assert: price_info::get_balance(price_info_object_X) == 2F.
8. Assert: no function exists to recover coin_F_2 for the caller.
```

The second fee `F` is permanently locked. Repeating step 5–6 `N` times locks `N*F` SUI with zero price updates delivered.

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-274)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L277-277)
```text
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L281-291)
```text
        let i = 0;
        let found = false;
        while (i < hot_potato_vector::length<PriceInfo>(&price_updates)){
            let cur_price_info = hot_potato_vector::borrow<PriceInfo>(&price_updates, i);
            if (has_same_price_identifier(cur_price_info, price_info_object)){
                found = true;
                update_cache(latest_only, cur_price_info, price_info_object, clock);
                break
            };
            i = i + 1;
        };
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

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-2)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
```
