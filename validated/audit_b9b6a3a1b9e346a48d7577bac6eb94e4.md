### Title
Attacker Can Steal Provider Fees via Unvalidated `providerToCredit` Parameter in `executeCallback()` - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `executeCallback()` function in `Echo.sol` accepts a caller-controlled `providerToCredit` parameter that is never validated against `msg.sender`. After the exclusivity period expires, any registered provider can call `executeCallback()` and redirect the full request fee to their own address, stealing revenue that should have gone to the originally assigned provider.

### Finding Description

In `Echo.sol`, `executeCallback()` is the function that fulfills a price-update request and credits the fulfilling provider's fee balance: [1](#0-0) 

The exclusivity check only enforces that `providerToCredit == req.provider` **during** the exclusivity window: [2](#0-1) 

Once `block.timestamp >= req.publishTime + _state.exclusivityPeriodSeconds`, the check is skipped entirely. The fee is then credited to the **attacker-supplied** `providerToCredit` with no validation that it equals `msg.sender` or that it is the legitimate assigned provider: [3](#0-2) 

There is also no check that `providerToCredit` is a registered provider at the point of crediting. The `registerProvider()` function is permissionless: [4](#0-3) 

`setFeeManager()` is also permissionless for any registered provider: [5](#0-4) 

Withdrawal is gated only on the fee manager check: [6](#0-5) 

**Full exploit path:**

1. Attacker calls `registerProvider(...)` — permissionless, no cost.
2. Attacker calls `setFeeManager(attackerAddress)` — sets themselves as their own fee manager.
3. Attacker monitors the chain for requests where `block.timestamp >= req.publishTime + exclusivityPeriodSeconds`.
4. Attacker fetches valid `updateData` for the required `priceIds` from the public Hermes API.
5. Attacker calls `executeCallback(attackerAddress, sequenceNumber, updateData, priceIds)` — passes their own address as `providerToCredit`.
6. `req.fee` (paid by the original requester, minus the Pyth protocol fee) is credited to the attacker's `accruedFeesInWei`.
7. Attacker calls `withdrawAsFeeManager(attackerAddress, amount)` and receives the stolen ETH.

The legitimate assigned provider receives nothing for the request they were supposed to fulfill.

### Impact Explanation

Direct theft of ETH fees from legitimate Echo providers. Every request whose exclusivity period has elapsed is vulnerable. The attacker only needs to cover the Pyth price-update fee (`pythFee`) as `msg.value`; they receive `req.fee + msg.value - pythFee` back. When `req.fee > pythFee` (the normal case, since `req.fee` includes the provider's base fee, per-feed fee, and gas fee), the attack is profitable. Providers lose all revenue from any request they fail to fulfill before the exclusivity window closes.

### Likelihood Explanation

The attack requires no privileged access. `registerProvider` is permissionless. Valid `updateData` is publicly available from Hermes. The attacker only needs to monitor the chain for expired exclusivity windows and submit a transaction. This is a straightforward MEV-style attack executable by any on-chain actor.

### Recommendation

Require that `providerToCredit == msg.sender` in `executeCallback()` after the exclusivity period, so only the actual transaction sender can claim the fee credit:

```solidity
if (block.timestamp >= req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == msg.sender,
        "providerToCredit must be msg.sender outside exclusivity period"
    );
}
```

Alternatively, remove the `providerToCredit` parameter entirely and always use `msg.sender` as the address to credit.

### Proof of Concept

1. Deploy Echo with `exclusivityPeriodSeconds = 30`.
2. Register `legitimateProvider` and `attacker` as providers; attacker calls `setFeeManager(attacker)`.
3. User calls `requestPriceUpdatesWithCallback(legitimateProvider, block.timestamp, priceIds, gasLimit)` paying `req.fee`.
4. Advance time by 31 seconds (exclusivity period expires).
5. Attacker fetches valid `updateData` from Hermes for the requested `priceIds`.
6. Attacker calls `executeCallback(attacker, sequenceNumber, updateData, priceIds)` with `msg.value >= pythFee`.
7. Assert: `echo.getProviderInfo(attacker).accruedFeesInWei == req.fee + msg.value - pythFee` (attacker received the fee).
8. Assert: `echo.getProviderInfo(legitimateProvider).accruedFeesInWei == 0` (legitimate provider received nothing).
9. Attacker calls `withdrawAsFeeManager(attacker, stolenAmount)` and receives the ETH.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-165)
```text
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

        clearRequest(sequenceNumber);

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-379)
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
    }
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
