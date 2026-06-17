Based on my full read of the relevant source files, here is my analysis:

**Tracing the fee flow:**

1. `update_single_price_feed` in `pyth.move` collects a `Coin<SUI>` fee and calls `price_info::deposit_fee_coins(price_info_object, fee)` at line 277. [1](#0-0) 

2. `deposit_fee_coins` in `price_info.move` either adds the coin as a new dynamic object field under `FEE_STORAGE_KEY`, or joins it into the existing one. There is no corresponding remove/withdraw function anywhere in this file. [2](#0-1) 

3. `get_balance` only reads the balance — it never removes or transfers coins. [3](#0-2) 

**Searching all governance and state modules for a withdrawal path:**

- `state.move` stores `fee_recipient_address` and exposes `set_fee_recipient` (friend-only), but has zero functions that call `dynamic_object_field::remove` or `coin::take` on `FEE_STORAGE_KEY`. [4](#0-3) 

- `governance.move` dispatches to: `set_governance_data_source`, `set_data_sources`, `set_update_fee`, `set_stale_price_threshold`, `set_fee_recipient`. None of these touch the fee balance inside a `PriceInfoObject`. [5](#0-4) 

- `set_fee_recipient.move` is the most telling: its own comment reads *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore. This module is kept for backward compatibility."* It only updates `fee_recipient_address` in `State` — it does not route or withdraw any coins. [6](#0-5) 

**Conclusion:**

The fee deposit path is fully functional and exercised by every `update_single_price_feed` call. The withdrawal path does not exist anywhere in the production codebase. The `fee_recipient_address` field in `State` is a dead remnant of a prior design. Every MIST paid as an update fee is permanently locked inside the corresponding `PriceInfoObject` with no recovery mechanism for the fee recipient or anyone else.

---

### Title
Permanent Locking of All Protocol Update Fees — No Withdrawal Path Exists for `Coin<SUI>` Stored Under `FEE_STORAGE_KEY` in `PriceInfoObject` — (`target_chains/sui/contracts/sources/price_info.move`)

### Summary
Every call to `update_single_price_feed` deposits the caller's fee `Coin<SUI>` into the target `PriceInfoObject` via `deposit_fee_coins`. No function in any module — public, entry, or governance — ever removes or transfers that coin out. All accumulated protocol fee revenue is permanently locked and irrecoverable.

### Finding Description
`pyth::pyth::update_single_price_feed` (pyth.move:277) calls `price_info::deposit_fee_coins`, which stores the fee coin as a dynamic object field under `FEE_STORAGE_KEY` on the `PriceInfoObject`. The only other function touching that field is `get_balance`, which is read-only. A search of all modules (`state.move`, `governance.move`, `set_fee_recipient.move`, `contract_upgrade.move`, `migrate.move`, and all governance action modules) confirms that `dynamic_object_field::remove` and `coin::take` are never called on `FEE_STORAGE_KEY`. The `fee_recipient_address` stored in `State` is never used to route fees; `set_fee_recipient.move` itself documents that this mechanism "is not used anymore."

### Impact Explanation
All `Coin<SUI>` paid as update fees by every user of the Sui Pyth deployment accumulates monotonically inside shared `PriceInfoObject` instances and can never be retrieved. The designated fee recipient receives nothing. The funds are not merely delayed — they are structurally unrecoverable without a contract upgrade that adds a new withdrawal entry point.

### Likelihood Explanation
This is triggered by normal, intended protocol usage. Every legitimate call to `update_single_price_feed` with a non-zero `base_update_fee` contributes to the locked balance. No attacker action is required; the locking is a consequence of the missing withdrawal path.

### Recommendation
Add a privileged (governance-gated or `fee_recipient`-only) entry function that calls `dynamic_object_field::remove<vector<u8>, Coin<SUI>>(&mut price_info_object.id, FEE_STORAGE_KEY)` and transfers the extracted coin to `state::get_fee_recipient(pyth_state)`. Alternatively, redesign fee collection to transfer coins directly to the fee recipient address at update time rather than storing them in the `PriceInfoObject`.

### Proof of Concept
1. Deploy the Sui Pyth contract with `base_update_fee = N > 0`.
2. Call `update_single_price_feed` with a valid price update and `fee = N` SUI.
3. Assert `price_info::get_balance(&price_info_object) == N`. ✓
4. Search all public/entry/governance functions for any call that decrements this balance. Result: none exist.
5. Repeat step 2 `k` times; assert `get_balance == k * N` with no corresponding decrease. The balance grows monotonically and is never transferred to `fee_recipient_address`.

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

**File:** target_chains/sui/contracts/sources/governance/governance.move (L99-114)
```text
        if (action == governance_action::new_contract_upgrade()) {
            abort(E_MUST_USE_CONTRACT_UPGRADE_MODULE_TO_DO_UPGRADES)
        } else if (action == governance_action::new_set_governance_data_source()) {
            set_governance_data_source::execute(&latest_only, pyth_state, governance_instruction::destroy(instruction));
        } else if (action == governance_action::new_set_data_sources()) {
            set_data_sources::execute(&latest_only, pyth_state, governance_instruction::destroy(instruction));
        } else if (action == governance_action::new_set_update_fee()) {
            set_update_fee::execute(&latest_only, pyth_state, governance_instruction::destroy(instruction));
        } else if (action == governance_action::new_set_stale_price_threshold()) {
            set_stale_price_threshold::execute(&latest_only, pyth_state, governance_instruction::destroy(instruction));
        } else if (action == governance_action::new_set_fee_recipient()) {
            set_fee_recipient::execute(&latest_only, pyth_state, governance_instruction::destroy(instruction));
        } else {
            governance_instruction::destroy(instruction);
            assert!(false, E_INVALID_GOVERNANCE_ACTION);
        }
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-18)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
module pyth::set_fee_recipient {
    use wormhole::cursor;
    use wormhole::external_address::{Self};

    use pyth::state::{Self, State, LatestOnly};

    friend pyth::governance;

    struct PythFeeRecipient {
        recipient: address
    }

    public(friend) fun execute(latest_only: &LatestOnly, state: &mut State, payload: vector<u8>) {
        let PythFeeRecipient { recipient } = from_byte_vec(payload);
        state::set_fee_recipient(latest_only, state, recipient);
    }
```
