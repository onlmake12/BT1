### Title
Missing Zero-Address Check on `providerToCredit` Allows Permanent Fee Locking - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `executeCallback` function in `Echo.sol` accepts a caller-supplied `providerToCredit` address with no zero-address validation. After the exclusivity period expires, any unprivileged caller can invoke `executeCallback` with `providerToCredit = address(0)`, crediting the request fee to `_state.providers[address(0)].accruedFeesInWei` — a balance that can never be withdrawn — permanently locking the requester's paid fee in the contract.

### Finding Description

`Echo.executeCallback` is a public function that fulfills a pending price-update request and credits the fee to a caller-chosen `providerToCredit`:

```solidity
function executeCallback(
    address providerToCredit,
    uint64 sequenceNumber,
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable override {
    ...
    if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
        require(
            providerToCredit == req.provider,
            "Only assigned provider during exclusivity period"
        );
    }
    ...
    _state.providers[providerToCredit].accruedFeesInWei += SafeCast
        .toUint128((req.fee + msg.value) - pythFee);
    ...
``` [1](#0-0) 

After the exclusivity window closes, the `providerToCredit == req.provider` guard is not enforced, and there is no `require(providerToCredit != address(0))` guard anywhere in the function. Fees credited to `address(0)` accumulate in `_state.providers[address(0)].accruedFeesInWei`.

The only withdrawal paths for provider fees are:

- `withdrawAsFeeManager(provider, amount)` — requires `msg.sender == _state.providers[provider].feeManager`. For `provider = address(0)`, `feeManager` is the zero-initialized default `address(0)`, so `msg.sender` can never satisfy this check.
- `withdrawFees(amount)` — admin-only, drains `_state.accruedFeesInWei` (Pyth protocol fees only, not provider fees). [2](#0-1) [3](#0-2) 

There is no direct `withdraw()` function for providers in `Echo.sol` (unlike `Entropy.sol`), so fees credited to `address(0)` are irrecoverable.

### Impact Explanation

- The legitimate provider loses their fee for executing the callback.
- The requester's fee (paid at request time via `msg.value`) is permanently locked in the contract with no recovery path.
- Repeated exploitation across many requests can drain significant value from the Echo fee pool into an unrecoverable state.

### Likelihood Explanation

- `executeCallback` is a permissionless public function callable by any address.
- The exclusivity period (`_state.exclusivityPeriodSeconds`) is a finite window; after it expires, any caller may supply `providerToCredit = address(0)`.
- No special privileges, leaked keys, or oracle manipulation are required.
- A griefing actor or competing relayer can front-run the legitimate provider's `executeCallback` transaction after the exclusivity period to steal the fee.

### Recommendation

Add a zero-address guard at the top of `executeCallback`:

```solidity
require(providerToCredit != address(0), "providerToCredit is zero address");
``` [4](#0-3) 

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit)` — fee is stored in `req.fee`.
2. Attacker waits until `block.timestamp >= req.publishTime + exclusivityPeriodSeconds`.
3. Attacker calls `executeCallback(address(0), sequenceNumber, updateData, priceIds)`.
4. The exclusivity check is skipped (period elapsed).
5. `_state.providers[address(0)].accruedFeesInWei += fee` — fee is credited to the zero-address slot.
6. No withdrawal function can drain `_state.providers[address(0)].accruedFeesInWei`; funds are permanently locked. [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-299)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
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
