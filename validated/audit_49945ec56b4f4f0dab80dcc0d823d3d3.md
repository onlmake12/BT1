Looking at the three key files to trace the exact code path.

### Title
Surplus SUI Permanently Locked in PriceInfoObject Due to Missing Refund and No Withdrawal Path — (`target_chains/sui/contracts/sources/pyth.move`, `price_info.move`)

---

### Summary

`update_single_price_feed` accepts any `Coin<SUI>` whose value is **≥** `base_update_fee` and deposits the **entire** coin into the `PriceInfoObject`. No change is returned and no withdrawal function exists anywhere in the module. Any amount paid above `base_update_fee` is permanently locked.

---

### Finding Description

**Step 1 — Fee guard is a lower-bound, not an equality check.** [1](#0-0) 

```move
assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);
// store fee coins within price info object
price_info::deposit_fee_coins(price_info_object, fee);
```

The guard only rejects coins whose value is *strictly less than* `base_update_fee`. Any coin with value `base_update_fee + N` (N > 0) passes the check, and the **whole** coin — including the surplus N — is forwarded to `deposit_fee_coins`.

**Step 2 — `deposit_fee_coins` is public and performs no splitting.** [2](#0-1) 

```move
public fun deposit_fee_coins(price_info_object: &mut PriceInfoObject, fee_coins: Coin<SUI>) {
    if (!dynamic_object_field::exists_with_type<...>(...)) {
        dynamic_object_field::add(&mut price_info_object.id, FEE_STORAGE_KEY, fee_coins);
    } else {
        let current_fee = dynamic_object_field::borrow_mut<...>(...);
        coin::join(current_fee, fee_coins);   // entire coin merged, no refund
    };
}
```

The function is `public` (callable by anyone), accepts an arbitrary `Coin<SUI>`, and unconditionally merges the full value into the `FEE_STORAGE_KEY` dynamic object field of the `PriceInfoObject`.

**Step 3 — No withdrawal path exists.**

A search across every `.move` file under `target_chains/sui/contracts/sources/` for `withdraw`, `collect_fee`, `transfer.*fee`, and `fee.*transfer` returns **zero matches**. The governance directory (`governance/`) contains `set_fee_recipient.move`, `set_update_fee.move`, and related files, but **no fee-collection or fee-extraction action**. `get_balance` is the only other function that touches `FEE_STORAGE_KEY`, and it is read-only. [3](#0-2) 

Once deposited, the SUI is irrecoverable: neither the user, nor the fee recipient, nor any governance action can extract it.

---

### Impact Explanation

Any caller of `update_single_price_feed` who passes a `Coin<SUI>` with value `> base_update_fee` permanently loses the surplus. Because `Coin<SUI>` in Sui is an object that must be fully consumed or returned, and because the protocol never splits the coin before depositing it, the surplus N SUI is merged into the `PriceInfoObject`'s dynamic field and has no exit path. This also means **all** collected fees (including the exact base fee) are permanently frozen — the `fee_recipient_address` stored in `State` is unreachable from `PriceInfoObject`. [4](#0-3) 

---

### Likelihood Explanation

Moderate. In Sui, callers must split coins manually before passing them. Any integrator or end-user who passes a wallet coin directly (a common pattern) without first splitting off exactly `base_update_fee` will silently lose the surplus. The `deposit_fee_coins` function being `public` also means a griefing path exists: anyone can call it directly to lock arbitrary SUI into any `PriceInfoObject`.

---

### Recommendation

1. **Split before deposit**: In `update_single_price_feed`, split the incoming coin to exactly `base_update_fee` and return the remainder to the caller:
   ```move
   let exact_fee = coin::split(&mut fee, state::get_base_update_fee(pyth_state), ctx);
   price_info::deposit_fee_coins(price_info_object, exact_fee);
   transfer::public_transfer(fee, tx_context::sender(ctx)); // return change
   ```
2. **Restrict `deposit_fee_coins`**: Change its visibility to `public(friend)` so only `pyth::pyth` can call it.
3. **Add a fee-collection function**: Implement a privileged `collect_fees(price_info_object, recipient)` callable by the fee recipient or governance to drain the `FEE_STORAGE_KEY` field.

---

### Proof of Concept

```move
// base_update_fee = 50 (DEFAULT_BASE_UPDATE_FEE in tests)
let oversized_fee = coin::mint_for_testing<SUI>(5000, ctx); // 100x the base fee
let vec = update_single_price_feed(
    &pyth_state,
    price_updates,
    &mut price_info_object,
    oversized_fee,   // passes the >= check, entire 5000 deposited
    &clock
);
// assert: 4950 SUI is permanently locked, unreachable
assert!(price_info::get_balance(&price_info_object) == 5000, 0);
// no function in price_info.move or any governance module can extract the 4950 surplus
```

The existing test at line 817 of `pyth.move` already asserts `get_balance == DEFAULT_BASE_UPDATE_FEE` after a correctly-sized payment, confirming the deposit path. Substituting an oversized coin reproduces the lock. [5](#0-4)

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L262-296)
```text
    public fun update_single_price_feed(
        pyth_state: &PythState,
        price_updates: HotPotatoVector<PriceInfo>,
        price_info_object: &mut PriceInfoObject,
        fee: Coin<SUI>,
        clock: &Clock
    ): HotPotatoVector<PriceInfo> {
        let latest_only = state::assert_latest_only(pyth_state);

        // On Sui, users get to choose which price feeds to update. They specify a single price feed to
        // update at a time. We therefore charge the base fee for each such individual update.
        // This is a departure from Eth, where users don't get to necessarily choose.
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);

        // Find price update corresponding to PriceInfoObject within the array of price_updates
        // and use it to update PriceInfoObject.
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
        if (found==false){
            abort E_PRICE_UPDATE_NOT_FOUND_FOR_PRICE_INFO_OBJECT
        };
        price_updates
    }
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
