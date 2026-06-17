### Title
Unvalidated `providerToCredit` in `executeCallback` Allows Fee Theft from Legitimate Providers — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `executeCallback` function in the `Echo` contract accepts a caller-supplied `providerToCredit` address and credits the full request fee to it without verifying that the address is the legitimate provider assigned to the request. After the exclusivity period expires, any unprivileged actor can call `executeCallback` with their own registered address as `providerToCredit`, stealing the fees that should accrue to the legitimate provider.

### Finding Description

`executeCallback` enforces `providerToCredit == req.provider` only during the exclusivity window:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

After that window closes, the check is skipped entirely. The fee is then unconditionally credited to whatever address the caller supplied:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

There is no subsequent validation that `providerToCredit` is the provider stored in the request, or even that it is a registered provider at all. The `ProviderInfo` mapping is a plain `mapping(address => ProviderInfo)`, so writing to an arbitrary address silently creates a new entry. [1](#0-0) [2](#0-1) 

The `withdrawAsFeeManager` function allows any address that is set as a provider's `feeManager` to drain that provider's `accruedFeesInWei`:

```solidity
function withdrawAsFeeManager(address provider, uint128 amount) external override {
    require(msg.sender == _state.providers[provider].feeManager, "Only fee manager");
    ...
    _state.providers[provider].accruedFeesInWei -= amount;
    (bool sent, ) = msg.sender.call{value: amount}("");
``` [3](#0-2) 

`registerProvider` is permissionless and `setFeeManager` allows any registered provider to designate any address (including themselves) as fee manager: [4](#0-3) [5](#0-4) 

### Impact Explanation

An attacker can steal the provider fee from every `Echo` request once its exclusivity period has elapsed. The stolen amount per request is `req.fee = msg.value - pythFeeInWei`, i.e., the full provider portion of the fee paid by the requester. Across many requests this constitutes a complete drain of all provider revenue accrued in the contract. [6](#0-5) 

### Likelihood Explanation

- `registerProvider` is open to anyone with no cost or stake.
- The default exclusivity period is 15 seconds; after that, any caller may invoke `executeCallback`.
- Valid `updateData` is freely available from the public Hermes API.
- No privileged key or governance access is required.
- The attack is repeatable for every outstanding request. [7](#0-6) 

### Recommendation

After the exclusivity period, restrict `providerToCredit` to the address stored in `req.provider`, or at minimum require that `providerToCredit` is a registered provider. The simplest fix is to remove the caller-supplied parameter entirely and always credit `req.provider`:

```solidity
// Replace the exclusivity block and the fee credit line with:
_state.providers[req.provider].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

If the intent is to allow a different provider to be credited after the exclusivity period (e.g., as a penalty mechanism), add an explicit allowlist or require `providerToCredit` to be a registered provider and emit an event recording the substitution.

### Proof of Concept

1. **Setup**: Attacker calls `registerProvider(0, 0, 0)` to register with zero fees, then calls `setFeeManager(attackerAddress)` to designate themselves as their own fee manager.

2. **Victim request**: A legitimate user calls `requestPriceUpdatesWithCallback(legitimateProvider, publishTime, priceIds, gasLimit)` paying fee `F`. The contract stores `req.provider = legitimateProvider` and `req.fee = F - pythFee`.

3. **Wait**: Attacker waits `exclusivityPeriodSeconds` (default 15 s) past `req.publishTime`.

4. **Exploit**: Attacker fetches valid `updateData` from Hermes and calls:
   ```solidity
   echo.executeCallback(
       attackerAddress,   // providerToCredit — attacker's own registered address
       sequenceNumber,
       updateData,
       priceIds
   );
   ```
   The exclusivity check is skipped. `_state.providers[attackerAddress].accruedFeesInWei` is incremented by `req.fee + msg.value - pythFee`.

5. **Drain**: Attacker calls `echo.withdrawAsFeeManager(attackerAddress, stolenAmount)` to transfer the ETH to themselves.

The legitimate provider (`legitimateProvider`) receives zero fees for the request they were assigned to fulfill. [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-164)
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L381-393)
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
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L31-46)
```text
    struct ProviderInfo {
        // Slot 1: 12 + 12 + 8 = 32 bytes
        uint96 baseFeeInWei;
        uint96 feePerFeedInWei;
        // 8 bytes padding

        // Slot 2: 12 + 16 + 4 = 32 bytes
        uint96 feePerGasInWei;
        uint128 accruedFeesInWei;
        // 4 bytes padding

        // Slot 3: 20 + 1 + 11 = 32 bytes
        address feeManager;
        bool isRegistered;
        // 11 bytes padding
    }
```
