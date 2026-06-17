The code evidence is conclusive. Let me summarize the findings across all relevant files.

**Fee deposit path** — `update_single_price_feed()` in `pyth.move` line 277 calls `price_info::deposit_fee_coins(price_info_object, fee)`, which either adds a new `Coin<SUI>` dynamic object field under `FEE_STORAGE_KEY` or joins it into the existing one. [1](#0-0) [2](#0-1) 

**No withdrawal path exists** — A full grep of all `.move` files for `dynamic_object_field::remove`, `coin::take`, and `withdraw` on `FEE_STORAGE_KEY` returns zero hits in the production Pyth contract sources. `get_balance()` can read the balance but there is no corresponding removal function. [3](#0-2) 

**`fee_recipient_address` is vestigial** — `state::set_fee_recipient()` writes to the field and `state::get_fee_recipient()` reads it, but neither is called from any transfer or withdrawal function anywhere in the codebase. [4](#0-3) [5](#0-4) 

**`set_fee_recipient.move` is explicitly deprecated** — The module header states the fee-recipient state "is not used anymore" and is "kept for backward compatibility only." [6](#0-5) 

---

### Title
Permanent Freezing of All Protocol Fee Yield — (`target_chains/sui/contracts/sources/price_info.move`, `pyth.move`)

### Summary
Every SUI fee paid through `update_single_price_feed()` is deposited into a `PriceInfoObject` dynamic object field under `FEE_STORAGE_KEY`. No function in the entire contract ever removes or transfers that coin. The `fee_recipient_address` field in `State` is written by a governance action explicitly marked as a backward-compatibility stub and is never read by any transfer logic. The accumulated balance is permanently irrecoverable.

### Finding Description
`pyth::update_single_price_feed()` calls `price_info::deposit_fee_coins()`, which stores the caller's `Coin<SUI>` as a dynamic object field (`FEE_STORAGE_KEY`) on the shared `PriceInfoObject`. The only operations ever performed on that field are `dynamic_object_field::add` (first deposit) and `coin::join` (subsequent deposits). `dynamic_object_field::remove`, `coin::take`, `transfer::public_transfer`, and any equivalent withdrawal primitive are absent from all production `.move` sources. The governance module `set_fee_recipient` updates `State.fee_recipient_address` but that address is never passed to a transfer call. The module comment confirms the recipient mechanism was intentionally removed.

### Impact Explanation
Every price-feed update fee paid on Sui is permanently locked inside the corresponding `PriceInfoObject`. Because `PriceInfoObject` is a shared object with no admin-controlled destructor or coin-extraction entry point, the SUI balance is irrecoverable on-chain. The total locked amount grows monotonically with protocol usage. This directly matches the "permanent freezing of unclaimed yield" impact category.

### Likelihood Explanation
The freezing is unconditional and begins with the very first `update_single_price_feed()` call on mainnet. No special attacker action is required; normal protocol operation is sufficient. The likelihood is therefore certain for any deployed instance that processes price updates.

### Recommendation
1. Add a privileged `withdraw_fees(price_info_object: &mut PriceInfoObject, recipient: address, ctx: &mut TxContext)` entry function (gated by `LatestOnly` or an admin capability) that calls `dynamic_object_field::remove<vector<u8>, Coin<SUI>>` on `FEE_STORAGE_KEY` and transfers the coin to `state::get_fee_recipient()`.
2. Alternatively, transfer fees directly to `fee_recipient_address` inside `deposit_fee_coins()` rather than accumulating them in the object.
3. Remove or repurpose the now-dead `set_fee_recipient` governance action once a real withdrawal path is wired up.

### Proof of Concept
```
// Invariant: sum of all PriceInfoObject balances is monotonically non-decreasing
// and no transaction can decrease it.
//
// 1. Call update_single_price_feed(..., fee_coins, ...) N times.
// 2. After each call, price_info::get_balance(pio) increases by coin::value(fee_coins).
// 3. Search entire contract for dynamic_object_field::remove on FEE_STORAGE_KEY → 0 results.
// 4. Search for any transfer of Coin<SUI> from PriceInfoObject → 0 results.
// 5. Conclusion: balance is strictly non-decreasing; no recovery path exists.
assert!(price_info::get_balance(&price_info_object_1) == DEFAULT_BASE_UPDATE_FEE, 0);
// (from existing test at pyth.move line 817 — already confirms deposit works;
//  no corresponding test or code path exists to withdraw)
``` [7](#0-6)

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-277)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L815-818)
```text

        // check fee coins are deposited in the price info object
        assert!(price_info::get_balance(&price_info_object_1)==DEFAULT_BASE_UPDATE_FEE, 0);

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

**File:** target_chains/sui/contracts/sources/state.move (L119-121)
```text
    public fun get_fee_recipient(s: &State): address {
        s.fee_recipient_address
    }
```

**File:** target_chains/sui/contracts/sources/state.move (L196-202)
```text
    public(friend) fun set_fee_recipient(
        _: &LatestOnly,
        self: &mut State,
        addr: address
    ) {
        self.fee_recipient_address = addr;
    }
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-3)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
module pyth::set_fee_recipient {
```
