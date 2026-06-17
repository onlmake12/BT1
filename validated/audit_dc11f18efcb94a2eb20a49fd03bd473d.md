### Title
`PythLazer.verifyUpdate` Uses `transfer()` for Excess-Fee Refund, Causing DoS for Smart Contract Callers - (`lazer/contracts/evm/src/PythLazer.sol`)

### Summary

`PythLazer.verifyUpdate` refunds excess ETH to the caller using Solidity's `transfer()`, which forwards only 2300 gas. Any smart contract caller whose `receive`/`fallback` function requires more than 2300 gas will have the entire `verifyUpdate` call revert, making it impossible for those contracts to consume Lazer price updates when they overpay.

### Finding Description

`PythLazer.verifyUpdate` is the entry point for Lazer price-update consumers. It requires `msg.value >= verification_fee` and refunds any excess:

```solidity
// lazer/contracts/evm/src/PythLazer.sol  lines 73-77
require(msg.value >= verification_fee, "Insufficient fee provided");
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
```

`transfer()` hard-caps the gas forwarded to the recipient at **2300**. Since EIP-1884 (Istanbul), many common operations (e.g., a single `SLOAD`, emitting an event, updating a mapping) cost more than 2300 gas. Any smart contract integrator whose `receive` or `fallback` function performs such operations will cause the `transfer()` to revert, which bubbles up and reverts the entire `verifyUpdate` call.

The root cause is the same vulnerability class as the reference report: the wrong transfer primitive is used for a fee/refund flow. In the reference report, `transfer` was used instead of `transferFrom` (wrong direction). Here, `transfer` is used instead of `call{value:…}("")` (wrong gas forwarding), causing the contract's own refund logic to break for a class of callers.

### Impact Explanation

Smart contract protocols that integrate PythLazer and call `verifyUpdate` with `msg.value > verification_fee` (a common defensive pattern to avoid "insufficient fee" reverts when the fee changes) will have every such call revert. The contract cannot receive the verified Lazer payload, breaking any on-chain logic that depends on it. The `verification_fee` is initialized to `1 wei`, so any caller sending even `2 wei` triggers the refund path.

### Likelihood Explanation

Smart contract integrators routinely overpay fees to guard against fee increases between the time they read the fee and the time the transaction lands. The `verification_fee` can be updated by the owner, making overpayment even more likely. Any integrator contract with a non-trivial `receive` function (e.g., one that emits an event or writes to storage) will be permanently unable to call `verifyUpdate` unless they send exactly `verification_fee`.

### Recommendation

Replace `transfer()` with a low-level `call` that forwards all available gas and checks the return value:

```solidity
if (msg.value > verification_fee) {
    (bool success, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(success, "Refund failed");
}
```

### Proof of Concept

1. Deploy a contract `Consumer` with a `receive()` function that emits an event (costs > 2300 gas).
2. Call `PythLazer.verifyUpdate{value: 2 wei}(validUpdate)` from `Consumer`.
3. The `transfer(1 wei)` to `Consumer` reverts because `Consumer.receive()` needs more than 2300 gas.
4. The entire `verifyUpdate` call reverts; `Consumer` cannot obtain the verified payload.
5. Calling `verifyUpdate{value: 1 wei}(validUpdate)` (exact fee) succeeds, confirming the refund path is the sole cause. [1](#0-0)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-77)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
