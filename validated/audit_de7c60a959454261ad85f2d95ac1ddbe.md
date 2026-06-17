### Title
Unregistered `providerToCredit` in `executeCallback` Permanently Locks Provider Fees — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.executeCallback` accepts a caller-supplied `providerToCredit` address and unconditionally credits the full request fee to `_state.providers[providerToCredit].accruedFeesInWei` without verifying that `providerToCredit` is a registered provider. After the exclusivity period expires, any unprivileged transaction sender can call `executeCallback` with an arbitrary unregistered address, permanently locking the provider fee in the contract with no withdrawal path.

### Finding Description

`executeCallback` enforces a registration check on the *requesting* provider only during the exclusivity window:

```solidity
if (block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds) {
    require(
        providerToCredit == req.provider,
        "Only assigned provider during exclusivity period"
    );
}
```

After the exclusivity period, the check is gone entirely. The fee credit then executes unconditionally:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
```

There is no `require(_state.providers[providerToCredit].isRegistered, ...)` guard here. If `providerToCredit` is an unregistered address, the fees land in a storage slot that has no withdrawal path:

- `withdrawAsFeeManager` requires `msg.sender == _state.providers[provider].feeManager`. For an unregistered address, `feeManager` is `address(0)`, so this can never be satisfied.
- `withdrawFees` (admin function) only drains `_state.accruedFeesInWei` (the Pyth protocol fee pool), not per-provider balances.
- Echo has no direct `withdraw()` function for providers (unlike Entropy).

The fees are permanently locked.

### Impact Explanation

Any pending request whose exclusivity period has elapsed can be fulfilled by an unprivileged caller passing an arbitrary unregistered address as `providerToCredit`. The legitimate provider (`req.provider`) loses their expected fee revenue, and the funds are irrecoverably locked in the contract. The attacker pays only gas; the loss falls entirely on the provider and the protocol.

### Likelihood Explanation

The exclusivity period is a configurable but finite window (default 15 seconds per the test suite). After it expires, the function is callable by anyone with valid `updateData`. A griefing actor monitoring the mempool can front-run the legitimate provider's `executeCallback` transaction and substitute an unregistered address. No privileged access, leaked key, or external oracle manipulation is required.

### Recommendation

Add a registration check on `providerToCredit` before crediting fees:

```solidity
require(
    _state.providers[providerToCredit].isRegistered,
    "providerToCredit is not a registered provider"
);
```

This mirrors the guard already present in `requestPriceUpdatesWithCallback` for the `provider` parameter.

### Proof of Concept

1. User calls `requestPriceUpdatesWithCallback(registeredProvider, publishTime, priceIds, gasLimit)` paying the required fee. The request is stored with `req.provider = registeredProvider` and `req.fee = msg.value - pythFeeInWei`.
2. The exclusivity period (`_state.exclusivityPeriodSeconds`, default 15 s) elapses.
3. Attacker calls `executeCallback(address(0xdead), sequenceNumber, updateData, priceIds)`. `address(0xdead)` is not registered.
4. The exclusivity check is skipped (timestamp past deadline).
5. `_state.providers[address(0xdead)].accruedFeesInWei += (req.fee + msg.value) - pythFee` executes.
6. `clearRequest(sequenceNumber)` removes the request.
7. `registeredProvider` never receives their fee. `address(0xdead)` has `feeManager == address(0)`, so `withdrawAsFeeManager` can never be called for it. The funds are permanently locked. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-61)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-121)
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-163)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

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
