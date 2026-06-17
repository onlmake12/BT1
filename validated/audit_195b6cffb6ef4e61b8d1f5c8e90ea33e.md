### Title
Indefinite Persistence of `CALLBACK_FAILED` Entropy Requests Enables Unauthorized Callback Re-execution — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `Entropy.sol` contract allows `revealWithCallback` to be called by **anyone** on a request in the `CALLBACK_FAILED` state, with no expiration or timeout. A failed callback request persists indefinitely on-chain. If a user's consumer contract initially has a failing callback (e.g., out-of-gas or a revert), makes a second corrected request that succeeds, and later fixes the root cause of the failure, an attacker can replay the old `CALLBACK_FAILED` request and force the callback to execute again with a stale random number — causing double-execution of application logic.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` accepts calls when `callbackStatus` is either `CALLBACK_NOT_STARTED` or `CALLBACK_FAILED`: [1](#0-0) 

When `callbackStatus == CALLBACK_FAILED`, the function takes the `else` branch, which clears the request and then calls the callback **directly** (no error catching): [2](#0-1) 

If the callback reverts, the entire transaction reverts (including `clearRequest`), so the request remains in `CALLBACK_FAILED` state permanently. There is **no expiration timestamp, no maximum retry count, and no mechanism for the requester to cancel a failed request**. The `isActive` check only tests `sequenceNumber != 0`: [3](#0-2) 

The comment in `allocRequest` explicitly acknowledges that requests can remain active forever: [4](#0-3) 

The `CALLBACK_FAILED` state is defined in `EntropyStatusConstants`: [5](#0-4) 

---

### Impact Explanation

An attacker who observes a `CALLBACK_FAILED` request on-chain can wait until the consumer contract's callback becomes executable (e.g., after the user upgrades their contract to fix the revert condition, or after a transient state dependency resolves), then call `revealWithCallback` on the old failed request. This forces the consumer's `_entropyCallback` to execute with a **stale random number** from the old request.

Concrete harms to the consumer contract:
- **Double-execution of game/lottery logic**: A game that distributes prizes in `entropyCallback` could pay out twice — once for the new request and once for the replayed old request.
- **NFT double-minting**: A mint-on-callback pattern would mint an extra token with the old random number.
- **State corruption**: Any stateful callback (e.g., setting a result, assigning a winner) could be overwritten with an old value.
- **Financial loss**: If the callback transfers funds or tokens, the consumer contract suffers an unintended transfer.

The attacker pays only gas; the consumer contract bears all the harm.

---

### Likelihood Explanation

- `revealWithCallback` is a **public, permissionless function** — any address can call it.
- `CALLBACK_FAILED` requests are **publicly visible on-chain** via `getRequestV2`.
- Upgradeable consumer contracts (a common pattern) are the primary target: a user deploys a proxy, the callback fails, they upgrade the implementation to fix the bug, and the old failed request immediately becomes exploitable.
- The Fortuna keeper itself retries failed requests automatically, meaning the infrastructure to discover and replay `CALLBACK_FAILED` requests already exists. [6](#0-5) 

---

### Recommendation

1. **Add an expiration timestamp** to each request (e.g., `expiryTime = block.timestamp + MAX_CALLBACK_RETRY_WINDOW`). Reject `revealWithCallback` calls on `CALLBACK_FAILED` requests after expiry.
2. **Allow the requester to cancel** a `CALLBACK_FAILED` request and receive a partial refund, analogous to the Scroll refund feature.
3. **Restrict who can retry** a `CALLBACK_FAILED` request to the original requester or the assigned provider, preventing arbitrary third-party replay.

---

### Proof of Concept

```solidity
// 1. Deploy a consumer whose callback initially reverts
contract VulnerableConsumer is IEntropyConsumer {
    bool public shouldRevert = true;
    uint64 public lastSeq;

    function requestRandom(IEntropy entropy, address provider) external payable {
        uint128 fee = entropy.getFeeV2(provider, 200_000);
        entropy.requestV2{value: fee}(provider, bytes32(0), 200_000);
    }

    function _entropyCallback(uint64 seq, address, bytes32) external override {
        require(!shouldRevert, "not ready");
        lastSeq = seq;
        // e.g., mint NFT, distribute prize, etc.
    }
    function getEntropy() internal view override returns (address) { ... }
}

// 2. Provider calls revealWithCallback → callback reverts → CALLBACK_FAILED
// 3. User fixes contract: shouldRevert = false
// 4. User makes a new request (seq=2), callback succeeds, lastSeq = 2
// 5. Attacker replays old CALLBACK_FAILED request (seq=1):
entropy.revealWithCallback(provider, seq1, userContrib1, providerContrib1);
// → _entropyCallback executes again with seq=1's stale random number
// → lastSeq = 1, overwriting the valid result; or NFT minted twice
```

The attacker entry path is: call `revealWithCallback(provider, failedSeqNum, userContrib, providerContrib)` — all parameters are recoverable from on-chain event logs (`RequestedWithCallback` and `CallbackFailed` events). [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-559)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-651)
```text
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1056-1064)
```text
            // There's already a prior active request in the storage slot we want to use.
            // Overflow the prior request to the requestsOverflow mapping.
            // It is important that this code overflows the *prior* request to the mapping, and not the new request.
            // There is a chance that some requests never get revealed and remain active forever. We do not want such
            // requests to fill up all of the space in the array and cause all new requests to incur the higher gas cost
            // of the mapping.
            //
            // This operation is expensive, but should be rare. If overflow happens frequently, increase
            // the size of the requests array to support more concurrent active requests.
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1071-1077)
```text
    function isActive(
        EntropyStructsV2.Request storage req
    ) internal view returns (bool) {
        // Note that a provider's initial registration occupies sequence number 0, so there is no way to construct
        // a randomness request with sequence number 0.
        return req.sequenceNumber != 0;
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStatusConstants.sol (L12-12)
```text
    uint8 public constant CALLBACK_FAILED = 3;
```

**File:** apps/fortuna/src/keeper/block.rs (L104-107)
```rust
/// Process a batch of blocks for a chain. It will fetch events for all the blocks in a single call for the provided batch
/// and then try to process them one by one. It checks the `fulfilled_request_cache`. If the request was already fulfilled.
/// It won't reprocess it. If the request was already processed, it will reprocess it.
/// If the process fails, it will retry indefinitely.
```
