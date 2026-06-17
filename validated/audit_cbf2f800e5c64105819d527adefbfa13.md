### Title
`.transfer()` Refund in `verifyUpdate()` Causes DoS for Contract-Based Lazer Updaters — (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.sol` uses `payable(msg.sender).transfer(...)` to refund excess ETH in `verifyUpdate()`. The `.transfer()` opcode forwards only 2300 gas. Any contract-based Lazer updater whose `receive()` or `fallback()` function consumes more than 2300 gas will cause the refund to revert, making the entire `verifyUpdate()` call fail. This is a receiver-mismatch analog to the reported missing-`receive()` class: instead of the audited contract being unable to accept ETH, the audited contract's refund path is unable to deliver ETH to callers with non-trivial receive logic.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate()` function is `payable` and refunds any overpayment to `msg.sender` using `.transfer()`:

```solidity
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [1](#0-0) 

`.transfer()` hard-caps the gas forwarded to the recipient at 2300. Any contract caller whose `receive()` or `fallback()` function performs even a single storage write, emits an event, or calls another contract will exceed 2300 gas. When that happens, `.transfer()` reverts, and because there is no try/catch around it, the entire `verifyUpdate()` call reverts. The caller's transaction fails, no price update is verified, and the caller's gas is consumed.

`PythLazer.sol` also has no `receive()` function of its own, so the contract cannot accept direct ETH injections outside of `verifyUpdate()`. Collected `verification_fee` amounts accumulate in the contract with no withdrawal path, but the more immediately exploitable issue is the refund DoS. [2](#0-1) 

### Impact Explanation
Any on-chain integration (e.g., a DeFi protocol, a keeper bot deployed as a contract, or a router contract) that calls `verifyUpdate()` and sends `msg.value > verification_fee` will have its call permanently revert if its `receive()` function uses more than 2300 gas. This silently breaks all contract-based Lazer consumers that do not send the exact fee amount. Because `verification_fee` is a public state variable that can be changed in a future upgrade, callers cannot reliably pre-compute the exact amount to send.

### Likelihood Explanation
Lazer updaters and relayers are the primary callers of `verifyUpdate()`. On-chain integrations (smart contracts acting as consumers or routers) are a realistic and expected caller class. Contracts that emit events or update state in their `receive()` function are common. The condition is triggered by any overpayment, which is a normal defensive pattern when fee amounts may change.

### Recommendation
Replace `.transfer()` with a low-level `.call{value:}("")` and check the return value:

```solidity
if (msg.value > verification_fee) {
    (bool ok, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(ok, "Refund failed");
}
```

This forwards all available gas to the recipient and avoids the 2300-gas stipend limitation.

### Proof of Concept
1. Deploy a contract `CallerContract` with a `receive()` function that emits an event (costs ~750 gas, but combined with other overhead exceeds 2300 gas in practice).
2. Call `CallerContract.doVerify{value: 2 wei}(pythLazerAddress, updateBytes)`.
3. Inside `doVerify`, call `pythLazer.verifyUpdate{value: 2 wei}(updateBytes)`.
4. `PythLazer` attempts `payable(msg.sender).transfer(1 wei)` (refunding 1 wei overpayment).
5. The transfer reverts because `CallerContract.receive()` exceeds 2300 gas.
6. The entire `verifyUpdate()` call reverts; no price update is verified. [3](#0-2)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-106)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }

        if (update.length < 71) {
            revert("input too short");
        }
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
        }
        uint16 payload_len = uint16(bytes2(update[69:71]));
        if (update.length < 71 + payload_len) {
            revert("input too short");
        }
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
        if (signer == address(0)) {
            revert("invalid signature");
        }
        if (!isValidSigner(signer)) {
            revert("invalid signer");
        }
    }
```
