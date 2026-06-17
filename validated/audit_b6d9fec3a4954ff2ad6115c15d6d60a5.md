Looking at the full call chain and all relevant production files:

**`update_single_price_feed`** in `pyth.move` line 277 calls `price_info::deposit_fee_coins(price_info_object, fee)`.

**`deposit_fee_coins`** in `price_info.move` lines 105–116 stores the `Coin<SUI>` as a dynamic object field under `FEE_STORAGE_KEY` on the `PriceInfoObject`'s `UID`. It only ever accumulates — `coin::join` merges subsequent deposits.

**No withdrawal path exists anywhere in the production contract set.** A full search across all `.move` files in `target_chains/sui/contracts/sources/` confirms there is no function that:
- removes the `FEE_STORAGE_KEY` dynamic field from a `PriceInfoObject`, or
- transfers accumulated `Coin<SUI>` to `fee_recipient_address`, or
- provides any governance action to drain `PriceInfoObject` balances.

**The governance action enum** in `governance_action.move` covers only: `CONTRACT_UPGRADE`, `SET_GOVERNANCE_DATA_SOURCE`, `SET_DATA_SOURCES`, `SET_UPDATE_FEE`, `SET_STALE_PRICE_THRESHOLD`, `SET_FEE_RECIPIENT`. None drain fees from `PriceInfoObject`.

**The `set_fee_recipient.move` module comment** (line 1) explicitly states: *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore. This module is kept for backward compatibility."* This confirms the fee-recipient mechanism was deprecated without providing an alternative collection path.

**`state.fee_recipient_address`** is stored and settable via governance, but is never read in any fee-transfer context in the production code.

---

### Title
Protocol Update Fees Permanently Locked in `PriceInfoObject` Shared Objects — No On-Chain Withdrawal Path (`target_chains/sui/contracts/sources/price_info.move`, `pyth.move`)

### Summary
Every call to `update_single_price_feed` deposits `Coin<SUI>` into the calling `PriceInfoObject` via `deposit_fee_coins`. There is no function — public, friend, or governance-gated — that can remove these coins. The `fee_recipient_address` stored in `State` is never used to receive fees. All protocol revenue is permanently locked.

### Finding Description
`update_single_price_feed` ( [1](#0-0) ) calls `price_info::deposit_fee_coins`, which stores the fee `Coin<SUI>` as a dynamic object field under `FEE_STORAGE_KEY` on the `PriceInfoObject`'s `UID`. [2](#0-1) 

Subsequent calls only `coin::join` into the existing balance — the balance grows monotonically with no removal path. [3](#0-2) 

`State` stores a `fee_recipient_address` field and exposes `get_fee_recipient`, but this address is never the target of any coin transfer in the production code. [4](#0-3) [5](#0-4) 

The `set_fee_recipient` governance module's own comment confirms the mechanism was abandoned: *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore."* [6](#0-5) 

The complete governance action set contains no "collect fees" or "drain PriceInfoObject" action. [7](#0-6) 

### Impact Explanation
All SUI fees paid by every caller of `update_single_price_feed` are permanently locked inside the respective `PriceInfoObject` shared objects. The protocol operator (`fee_recipient_address`) receives zero revenue. The locked funds cannot be recovered without a contract upgrade that adds a withdrawal function. This is a direct, concrete, permanent loss of protocol funds — not a theoretical risk.

### Likelihood Explanation
This triggers on every legitimate price update call. No special attacker action is required; normal protocol operation causes the invariant to be violated. The `DEFAULT_BASE_UPDATE_FEE` in tests is 50 MIST per update, and the fee is configurable upward via governance. On a live deployment with real update volume, the locked amount grows continuously.

### Recommendation
Add a privileged (governance-gated or admin-only) function that:
1. Borrows and removes the `FEE_STORAGE_KEY` dynamic object field from a `PriceInfoObject`.
2. Transfers the extracted `Coin<SUI>` to `state::get_fee_recipient(pyth_state)`.

Alternatively, redesign `update_single_price_feed` to transfer the fee coin directly to `fee_recipient_address` at call time using `sui::transfer::public_transfer`, bypassing the `PriceInfoObject` accumulation entirely — which appears to have been the original design intent before the refactor.

### Proof of Concept
Invariant test (pseudocode):
```
// 1. Deploy Pyth with base_update_fee = 50, fee_recipient = RECIPIENT
// 2. Call update_single_price_feed N times, each with fee = 50 SUI
// 3. Assert: balance(RECIPIENT) == 0          // fee_recipient received nothing
// 4. Assert: price_info::get_balance(price_info_object) == N * 50  // all locked
// 5. Assert: no callable function exists to move coins to RECIPIENT
//    => invariant broken: protocol funds permanently inaccessible
``` [8](#0-7) [9](#0-8)

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

**File:** target_chains/sui/contracts/sources/governance/governance_action.move (L1-10)
```text
module pyth::governance_action {

    const CONTRACT_UPGRADE: u8 = 0;
    const SET_GOVERNANCE_DATA_SOURCE: u8 = 1;
    const SET_DATA_SOURCES: u8 = 2;
    const SET_UPDATE_FEE: u8 = 3;
    const SET_STALE_PRICE_THRESHOLD: u8 = 4;
    const SET_FEE_RECIPIENT: u8 = 5;

    const E_INVALID_GOVERNANCE_ACTION: u64 = 6;
```
