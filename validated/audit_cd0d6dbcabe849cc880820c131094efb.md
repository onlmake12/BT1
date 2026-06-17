### Title
Unauthorized `providerToCredit` in `Echo.executeCallback` Allows Fee Theft After Exclusivity Period - (File: target_chains/ethereum/contracts/contracts/echo/Echo.sol)

### Summary
`Echo.executeCallback` accepts a caller-controlled `providerToCredit` address with no validation after the exclusivity window expires. Any attacker who pre-registers as a provider can front-run a legitimate provider's fulfillment transaction, redirect the entire request fee to themselves, and withdraw it — stealing the fee that was paid by the requester and owed to the assigned provider.

### Finding Description
`Echo.requestPriceUpdatesWithCallback` stores the assigned provider in `req.provider` and records the fee paid by the requester in `req.fee`. When `executeCallback` is called to fulfill the request, it accepts a `providerToCredit` parameter that determines who receives `req.fee`.

During the exclusivity window (`block.timestamp < req.publishTime + exclusivityPeriodSeconds`), the contract enforces that `providerToCredit == req.provider`. However, once the exclusivity period expires, this check is skipped entirely: [1](#0-0) 

After that guard, the fee is unconditionally credited to the caller-supplied address: [2](#0-1) 

There is no check that `providerToCredit` is:
- the actual `msg.sender` performing the work,
- the originally assigned `req.provider`, or
- even a registered provider.

Provider registration is permissionless — anyone can call `registerProvider` with zero fees: [3](#0-2) 

And `setFeeManager` allows a registered provider to designate themselves as their own fee manager: [4](#0-3) 

Withdrawal is then available via `withdrawAsFeeManager`: [5](#0-4) 

### Impact Explanation
An attacker can steal 100% of the provider fee from every request whose exclusivity period has elapsed. The fee (`req.fee`) was paid by the requester and is the economic incentive for the assigned provider to fulfill the request. Redirecting it to an attacker:

1. Denies the legitimate provider their earned fee.
2. Undermines the economic incentive for providers to fulfill requests promptly, degrading service reliability.
3. Allows a single attacker to drain all pending provider fees across all open requests simultaneously by front-running fulfillment transactions.

The Pyth protocol fee (`_state.pythFeeInWei`) is still correctly accrued to `_state.accruedFeesInWei` before the credit, so only the provider portion is stolen. [6](#0-5) 

### Likelihood Explanation
- The attack requires no privileged access — only a pre-registered provider address (permissionless) and the ability to observe and front-run pending `executeCallback` transactions in the mempool.
- The attacker can copy `updateData` and `priceIds` verbatim from the victim's pending transaction, requiring zero additional off-chain infrastructure.
- Every request whose exclusivity period has elapsed is a target. On a busy chain, this is a continuous, automated, low-cost attack.
- The exclusivity period is a configurable admin parameter; if set to zero, every request is immediately vulnerable. [1](#0-0) 

### Recommendation
Validate `providerToCredit` in `executeCallback` after the exclusivity period. The simplest fix is to require `providerToCredit == msg.sender`, ensuring only the actual transaction sender can claim the fee. Alternatively, maintain a whitelist of approved providers (analogous to the recommendation in the reference report) and restrict `providerToCredit` to that set. At minimum, require `_state.providers[providerToCredit].isRegistered` to prevent crediting arbitrary addresses.

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

// Setup (done once, before any requests):
// 1. Attacker registers as a provider with zero fees
echo.registerProvider(0, 0, 0);          // permissionless
echo.setFeeManager(attacker);            // attacker is their own fee manager

// Normal flow: requester creates a request for defaultProvider
uint64 seq = echo.requestPriceUpdatesWithCallback{value: totalFee}(
    defaultProvider, publishTime, priceIds, callbackGasLimit
);
// req.fee = totalFee - pythFee  (owed to defaultProvider)

// Wait for exclusivity period to expire:
// block.timestamp >= req.publishTime + exclusivityPeriodSeconds

// Attack: attacker front-runs defaultProvider's executeCallback tx.
// Attacker copies updateData and priceIds from defaultProvider's pending tx,
// but substitutes providerToCredit = attacker.
echo.executeCallback(
    attacker,      // <-- providerToCredit: no validation after exclusivity
    seq,
    updateData,    // copied from defaultProvider's pending tx
    priceIds
);
// Result: _state.providers[attacker].accruedFeesInWei += req.fee
// defaultProvider receives nothing.

// Attacker withdraws stolen fee:
echo.withdrawAsFeeManager(attacker, stolenAmount);
```

The callback to the requester still executes successfully (line 176–201), so the service appears to function normally while the fee is silently redirected. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-99)
```text
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-162)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );

        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L350-358)
```text
    function setFeeManager(address manager) external override {
        require(
            _state.providers[msg.sender].isRegistered,
            "Provider not registered"
        );
        address oldFeeManager = _state.providers[msg.sender].feeManager;
        _state.providers[msg.sender].feeManager = manager;
        emit FeeManagerUpdated(msg.sender, oldFeeManager, manager);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-378)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-392)
```text
    function registerProvider(
        uint96 baseFeeInWei,
        uint96 feePerFeedInWei,
        uint96 feePerGasInWei
    ) external override {
        ProviderInfo storage provider = _state.providers[msg.sender];
        require(!provider.isRegistered, "Provider already registered");
        provider.baseFeeInWei = baseFeeInWei;
        provider.feePerFeedInWei = feePerFeedInWei;
        provider.feePerGasInWei = feePerGasInWei;
        provider.isRegistered = true;
        emit ProviderRegistered(msg.sender, feePerGasInWei);
```
