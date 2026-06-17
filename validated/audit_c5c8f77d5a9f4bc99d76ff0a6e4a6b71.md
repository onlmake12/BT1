### Title
Front-Running of `executeCallback` Fee Credit via Attacker-Controlled `providerToCredit` Parameter - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.sol`'s `executeCallback` function accepts a caller-supplied `providerToCredit` address and credits the full request fee to that address with no authentication after the exclusivity period expires. Any unprivileged actor can observe a legitimate provider's pending `executeCallback` transaction in the mempool and front-run it — substituting their own address as `providerToCredit` — to steal the fee that should have gone to the legitimate provider.

### Finding Description

`executeCallback` is a permissionless external function. Its only access control is an exclusivity window:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

Once `block.timestamp >= req.publishTime + exclusivityPeriodSeconds`, the check is skipped entirely. The fee is then credited unconditionally to the caller-supplied address:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

There is no verification that `providerToCredit == msg.sender`, `providerToCredit == req.provider`, or even that `providerToCredit` is a registered provider. The attacker-controlled value flows directly into the fee accounting state. [1](#0-0) [2](#0-1) 

### Impact Explanation

A malicious actor who:
1. Registers as a provider (permissionless, `registerProvider` has no gatekeeping),
2. Sets themselves as their own fee manager via `setFeeManager`,
3. Monitors the mempool for a legitimate provider's `executeCallback` transaction,
4. Resubmits the identical calldata with `providerToCredit = attackerAddress` at a higher gas price,

…will have the entire `req.fee` (the fee paid by the user at request time, minus the Pyth protocol cut) credited to their account. They then drain it via `withdrawAsFeeManager`. The legitimate provider receives nothing despite having set up infrastructure and being the assigned fulfiller. [3](#0-2) [4](#0-3) 

### Likelihood Explanation

- The exclusivity period is the only barrier; once it elapses the attack window is open for every unfulfilled request.
- All inputs needed to replicate the call (`sequenceNumber`, `updateData`, `priceIds`) are fully visible on-chain or in the mempool — no secret knowledge is required.
- Registering as a provider is permissionless and costs only gas.
- MEV bots routinely monitor mempools for exactly this pattern (profitable permissionless calls). The attack is straightforward to automate. [5](#0-4) 

### Recommendation

Replace the caller-supplied `providerToCredit` parameter with `msg.sender` for fee attribution, or add an explicit check that `providerToCredit == msg.sender`. This ensures only the actual transaction submitter can claim the fee, eliminating the front-running surface:

```solidity
// Instead of accepting providerToCredit as a parameter:
_state.providers[msg.sender].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

Alternatively, if third-party relayers must be supported, bind `providerToCredit` to `req.provider` at all times (not just during the exclusivity window), and introduce a separate, authenticated relayer-reward mechanism.

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback(legitimateProvider, publishTime, priceIds, gasLimit)` paying fee `F`. The request is stored with `req.provider = legitimateProvider` and `req.fee = F - pythFee`.

2. The exclusivity period (`exclusivityPeriodSeconds`) elapses.

3. `legitimateProvider`'s keeper broadcasts:
   ```
   executeCallback(legitimateProvider, seqNum, updateData, priceIds)
   ```

4. Attacker (already registered as a provider with themselves as fee manager) sees this in the mempool and broadcasts with higher gas:
   ```
   executeCallback(attackerAddress, seqNum, updateData, priceIds)
   ```

5. Attacker's transaction is mined first. Line 161 executes:
   ```solidity
   _state.providers[attackerAddress].accruedFeesInWei += (req.fee + msg.value) - pythFee;
   ```
   `legitimateProvider.accruedFeesInWei` is unchanged. The request is cleared.

6. `legitimateProvider`'s transaction reverts with `NoSuchRequest` (request already cleared).

7. Attacker calls `withdrawAsFeeManager(attackerAddress, stolenAmount)` and receives the full fee. [6](#0-5)

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
