### Title
ETH Refund via `transfer()` in `verifyUpdate()` Reverts for Contract Callers - (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate()` uses `payable(msg.sender).transfer(...)` to refund excess ETH to callers. The `.transfer()` opcode forwards only 2300 gas, which is insufficient for any contract recipient with non-trivial `receive()` or `fallback()` logic. This causes the entire `verifyUpdate()` call to revert for contract integrators that overpay, making PythLazer unusable for a broad class of on-chain consumers.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate()` function collects a `verification_fee` and refunds any excess `msg.value` to the caller:

```solidity
// lazer/contracts/evm/src/PythLazer.sol, lines 74-77
require(msg.value >= verification_fee, "Insufficient fee provided");
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
```

`address.transfer()` hard-caps the gas forwarded to the recipient at 2300. This is sufficient only for EOAs or contracts with empty `receive()` functions. Any contract that:
- Uses a multisig wallet (e.g., Gnosis Safe),
- Has a non-trivial `receive()` or `fallback()` (e.g., emits an event, updates state, or delegates),
- Is a proxy contract,

…will cause `.transfer()` to revert, which propagates and reverts the entire `verifyUpdate()` call. The caller loses no funds (the transaction reverts), but they are **permanently unable to call `verifyUpdate()` while sending any excess ETH**, effectively bricking PythLazer integration for those contracts.

The `verification_fee` is a mutable governance parameter (`verification_fee` is a public `uint256` set at initialization and can be changed by the owner). Contract integrators that cache the fee or compute it slightly differently may routinely send a small excess, triggering this revert path on every call.

### Impact Explanation
Any contract caller that sends `msg.value > verification_fee` to `verifyUpdate()` will have the transaction revert due to the 2300-gas `.transfer()` stipend being insufficient for contract recipients. Since the primary consumers of Pyth Lazer are DeFi protocols (contracts), this is a functional DoS for a large class of legitimate integrators. The contract cannot receive its price update, and the fee is not consumed (transaction reverts), but the integration is broken.

### Likelihood Explanation
High. DeFi protocols are the primary target audience for Pyth Lazer. Contracts routinely overpay fees as a safety margin, especially when `verification_fee` can change between the time a fee is queried and the time the transaction is mined. Multisig-controlled contracts (Gnosis Safe, etc.) are extremely common in DeFi and universally fail the 2300-gas `.transfer()` check.

### Recommendation
Replace `payable(msg.sender).transfer(...)` with a low-level `.call{value: ...}("")` pattern, checking the return value:

```solidity
if (msg.value > verification_fee) {
    (bool success, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(success, "ETH refund failed");
}
```

This forwards all available gas to the recipient, allowing contract wallets and proxy contracts to receive the refund without reverting.

### Proof of Concept
1. Deploy a contract `ContractCaller` with a `receive()` function that emits an event (costs >2300 gas).
2. Call `PythLazer.verifyUpdate{value: verification_fee + 1 wei}(validUpdate)` from `ContractCaller`.
3. The `payable(msg.sender).transfer(1 wei)` call at line 76 forwards only 2300 gas to `ContractCaller.receive()`.
4. `ContractCaller.receive()` runs out of gas, causing `.transfer()` to revert.
5. The entire `verifyUpdate()` transaction reverts — the price update is never delivered. [1](#0-0) [2](#0-1)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L10-11)
```text
    uint256 public verification_fee;
    mapping(address => uint256) trustedSignerToExpiresAtMapping;
```

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
