### Title
Provider Fee Credited Before Callback Fulfillment Allows Provider to Drain Fees and Leave Callbacks Permanently Unfulfilled — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
In `Entropy.sol`, the provider fee (including the gas-cost portion for the callback) is credited to `providerInfo.accruedFeesInWei` immediately at request time, before the callback is ever fulfilled. The `withdraw()` and `withdrawAsFeeManager()` functions impose no check for pending unfulfilled callback requests. A registered provider can therefore accept callback requests, immediately drain all accrued fees, and never fulfill the callbacks — leaving users with permanently stuck requests and no refund path.

### Finding Description
In `requestHelper()`, the provider fee is credited unconditionally at the moment the user submits a request:

```solidity
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;   // line 237 — credited before any fulfillment
``` [1](#0-0) 

The contract simultaneously records that the callback has not yet been executed:

```solidity
req.callbackStatus = isRequestWithCallback
    ? EntropyStatusConstants.CALLBACK_NOT_STARTED
    : EntropyStatusConstants.CALLBACK_NOT_NECESSARY;
``` [2](#0-1) 

Yet `withdraw()` and `withdrawAsFeeManager()` contain no guard against pending `CALLBACK_NOT_STARTED` requests:

```solidity
function withdraw(uint128 amount) public override {
    require(providerInfo.accruedFeesInWei >= amount, "Insufficient balance");
    providerInfo.accruedFeesInWei -= amount;
    (bool sent, ) = msg.sender.call{value: amount}("");
    ...
}
``` [3](#0-2) 

```solidity
function withdrawAsFeeManager(address provider, uint128 amount) external override {
    ...
    require(providerInfo.accruedFeesInWei >= amount, "Insufficient balance");
    providerInfo.accruedFeesInWei -= amount;
    ...
}
``` [4](#0-3) 

The off-chain keeper (Fortuna) funds its gas wallet by withdrawing from `accruedFeesInWei`. If the provider withdraws all fees before the keeper fulfills pending callbacks, the keeper has no ETH to pay callback gas, and the `CALLBACK_NOT_STARTED` requests are permanently stuck. [5](#0-4) 

### Impact Explanation
Users who call `requestV2{value: fee}()` with a callback pay a fee that includes the gas cost for the callback. That fee is immediately available to the provider. If the provider withdraws all fees before the keeper fulfills the callback, the user's randomness request is permanently unfulfilled with no on-chain refund mechanism. Any application relying on the `entropyCallback` (e.g., a coin-flip contract, a lottery, a game) will be permanently broken for those sequence numbers.

### Likelihood Explanation
Registration as an entropy provider is permissionless — any address can call `register()`. A malicious provider can:
1. Register with a valid hash chain.
2. Attract users (or be set as the default provider).
3. Immediately call `withdraw()` after each batch of requests to drain `accruedFeesInWei`.
4. Never run a keeper, leaving all `isRequestWithCallback` requests in `CALLBACK_NOT_STARTED` state.

The attack requires no privileged access beyond being a registered provider, which is open to anyone.

### Recommendation
Track the total fees attributable to pending callback requests in a separate `pendingCallbackFeesInWei` counter. Prevent `withdraw()` and `withdrawAsFeeManager()` from reducing `accruedFeesInWei` below `pendingCallbackFeesInWei`. Decrement `pendingCallbackFeesInWei` only when a callback is successfully fulfilled (or when a request is cancelled/refunded). This ensures that fees earmarked for callback gas cannot be withdrawn before the obligation is settled — directly mirroring the fix recommended in the original report.

### Proof of Concept
1. Attacker registers as a provider: `entropy.register(feeInWei, commitment, metadata, chainLength, uri)`.
2. User calls `entropy.requestV2{value: fee}(callbackGasLimit)` — `providerInfo.accruedFeesInWei += providerFee` executes immediately; `req.callbackStatus = CALLBACK_NOT_STARTED`.
3. Attacker immediately calls `entropy.withdraw(providerInfo.accruedFeesInWei)` — all fees drained; no check for pending callbacks.
4. No keeper is run; `revealWithCallback()` is never called.
5. User's `entropyCallback` is never invoked; the request is permanently stuck in `CALLBACK_NOT_STARTED` with no refund path. [6](#0-5) [1](#0-0)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-173)
```text
    function withdraw(uint128 amount) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            msg.sender
        ];

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(msg.sender, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            msg.sender,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L175-209)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(provider, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            provider,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-239)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L265-267)
```text
        req.callbackStatus = isRequestWithCallback
            ? EntropyStatusConstants.CALLBACK_NOT_STARTED
            : EntropyStatusConstants.CALLBACK_NOT_NECESSARY;
```

**File:** apps/fortuna/src/keeper/fee.rs (L91-140)
```rust
#[tracing::instrument(name = "withdraw_fees", skip_all, fields())]
pub async fn withdraw_fees_wrapper(
    contract_as_fee_manager: Arc<InstrumentedSignablePythContract>,
    provider_address: Address,
    poll_interval: Duration,
    min_balance: U256,
    keeper_address: Address,
    other_keeper_addresses: Vec<Address>,
) {
    let fee_manager_wallet = contract_as_fee_manager.wallet().address();

    // Add the fee manager to the list of other keepers so that we can fairly distribute the fees
    // across the fee manager and all the keepers.
    let mut other_keepers_and_fee_mgr = other_keeper_addresses.clone();
    other_keepers_and_fee_mgr.push(contract_as_fee_manager.wallet().address());

    loop {
        // Top up the fee manager balance
        // Do this before attempting to top up the keeper balance, since we need a funded
        // fee manager to be able to withdraw & transfer funds to the keeper.
        if let Err(e) = withdraw_fees_if_necessary(
            contract_as_fee_manager.clone(),
            provider_address,
            fee_manager_wallet,
            other_keepers_and_fee_mgr.clone(),
            min_balance,
        )
        .in_current_span()
        .await
        {
            tracing::error!("Withdrawing fees to fee manager. error: {:?}", e);
        }

        // Top up the keeper balance
        if let Err(e) = withdraw_fees_if_necessary(
            contract_as_fee_manager.clone(),
            provider_address,
            keeper_address,
            other_keepers_and_fee_mgr.clone(),
            min_balance,
        )
        .in_current_span()
        .await
        {
            tracing::error!("Withdrawing fees to keeper. error: {:?}", e);
        }

        time::sleep(poll_interval).await;
    }
}
```
