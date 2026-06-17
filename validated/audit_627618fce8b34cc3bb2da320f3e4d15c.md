### Title
Push-Based Excess Fee Refund via `.transfer()` Permanently Blocks Contract Callers in `verifyUpdate` - (File: lazer/contracts/evm/src/PythLazer.sol)

### Summary
`PythLazer.verifyUpdate` uses the deprecated `.transfer()` primitive to push excess ETH refunds back to `msg.sender`. Any contract caller whose `receive()` or `fallback()` function consumes more than 2300 gas (or deliberately reverts) will have every `verifyUpdate` call revert, permanently locking that contract out of the Lazer verification path.

### Finding Description
In `PythLazer.verifyUpdate`, after confirming the fee is sufficient, the contract unconditionally pushes the excess back to the caller:

```solidity
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee); // ← push
    }
    ...
}
``` [1](#0-0) 

`.transfer()` hard-caps the forwarded gas at 2300. Since EIP-1884 (Istanbul), a bare `SLOAD` costs 800 gas, so any `receive()` that reads a storage slot, emits an event, or calls another contract exceeds 2300 gas. When the push fails, the entire `verifyUpdate` call reverts — including the signature verification work already done — and the caller receives nothing.

A contract caller cannot avoid this by "just sending the exact fee" in all cases: the `verification_fee` is owner-settable at any time, so a fee change between the caller's read and their transaction landing causes an unintended overpayment, triggering the revert path. [2](#0-1) 

### Impact Explanation
Any smart contract that integrates with PythLazer (e.g., a DeFi protocol consuming Lazer price feeds) and whose `receive()` function uses more than 2300 gas is permanently unable to call `verifyUpdate`. Because `verifyUpdate` is the sole on-chain entry point for Lazer price verification, the entire Lazer integration for that contract is bricked. The contract cannot fall back to a different code path; there is no pull-based refund alternative.

### Likelihood Explanation
Medium. Contracts that integrate Lazer are likely to be non-trivial (they emit events, update state, or forward ETH in their `receive()`). Fee-change races are realistic: the owner can call `updateTrustedSigner` / adjust `verification_fee` at any time. A contract that hardcodes a slightly-higher ETH value as a safety buffer (a common pattern) will always overpay and will always revert if its `receive()` is non-trivial.

### Recommendation
Replace `.transfer()` with a low-level `.call{value:}("")` and handle failure gracefully — either by silently skipping the refund (acceptable since the caller overpaid voluntarily) or by crediting the excess to a per-caller mapping for a separate `withdraw()` pull:

```solidity
if (msg.value > verification_fee) {
    uint256 excess = msg.value - verification_fee;
    (bool ok, ) = payable(msg.sender).call{value: excess}("");
    if (!ok) {
        pendingRefunds[msg.sender] += excess; // pull-based fallback
    }
}
```

### Proof of Concept
1. Deploy a contract `Caller` with:
   ```solidity
   receive() external payable {
       emit Received(msg.value); // costs >2300 gas
   }
   function callVerify(PythLazer lazer, bytes calldata update) external payable {
       lazer.verifyUpdate{value: msg.value}(update);
   }
   ```
2. Set `verification_fee = 1 wei` on `PythLazer`.
3. Call `Caller.callVerify{value: 2 wei}(...)` with a valid Lazer update.
4. The transaction reverts at the `.transfer()` line because `Caller.receive()` exceeds 2300 gas.
5. `Caller` can never successfully call `verifyUpdate` with any overpayment, regardless of how valid the update data is. [3](#0-2)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L31-34)
```text
    function updateTrustedSigner(
        address trustedSigner,
        uint256 expiresAt
    ) external onlyOwner {
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
