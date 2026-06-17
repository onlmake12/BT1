### Title
`blockhash(req.blockNumber)` Always Returns Zero on Arbitrum/Optimism, Permanently Bricking `useBlockhash` Requests - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `Entropy` contract stores `block.number` at request time and later calls `blockhash(req.blockNumber)` during reveal. On Arbitrum, `block.number` returns the L1 block number while `blockhash()` looks up L2 block hashes — a permanent mismatch that always returns `bytes32(0)`. On Optimism, each transaction is its own block, so the 256-block window for `blockhash()` expires in seconds. In both cases, any user who calls the legacy `request()` function with `useBlockHash = true` on these chains will have their reveal permanently revert with `BlockhashUnavailable`, losing their paid fee with no recourse.

---

### Finding Description

In `requestHelper()`, the current `block.number` is stored into the request struct:

```solidity
req.blockNumber = SafeCast.toUint64(block.number);
req.useBlockhash = useBlockhash;
``` [1](#0-0) 

Later, in `revealHelper()`, when `req.useBlockhash == true`, the contract calls:

```solidity
bytes32 _blockHash = blockhash(req.blockNumber);
if (_blockHash == bytes32(uint256(0)))
    revert EntropyErrors.BlockhashUnavailable();
``` [2](#0-1) 

The `request()` function (the legacy public entry point) allows any caller to set `useBlockHash = true`:

```solidity
function request(
    address provider,
    bytes32 userCommitment,
    bool useBlockHash
) public payable override returns (uint64 assignedSequenceNumber) {
``` [3](#0-2) 

**On Arbitrum**: Per Arbitrum documentation, `block.number` returns the most recently synced **L1** block number (updated once per minute). However, `blockhash(n)` on Arbitrum returns the hash of **L2** block `n`. Since `req.blockNumber` holds an L1 block number (e.g., 18,000,000+), but `blockhash()` is indexing into L2 block history, the L2 block at that index either does not exist or is far outside the 256-block window. `blockhash()` returns `bytes32(0)` unconditionally, causing every `reveal()` call to revert with `BlockhashUnavailable`.

**On Optimism**: Each transaction is its own L2 block. The 256-block window for `blockhash()` therefore spans only ~256 transactions on the network, which can expire in seconds under normal load. A user who does not reveal within that tiny window is permanently locked out.

The `EntropyStructsV2.Request` struct confirms `blockNumber` is stored as a `uint64` with the explicit comment that it is the block number at request time: [4](#0-3) 

---

### Impact Explanation

A user who calls `request(provider, commitment, true)` on Arbitrum or Optimism:
1. Pays the provider fee + Pyth protocol fee in native token (non-refundable).
2. Receives a sequence number.
3. Can **never** successfully call `reveal()` — every attempt reverts with `BlockhashUnavailable`.
4. The request slot remains permanently active (sequence number consumed, storage occupied), and the user's fee is permanently lost.

This is a direct loss of user funds with no recovery path, since there is no timeout-based refund mechanism in the contract.

---

### Likelihood Explanation

- The Entropy contract is deployed on multiple EVM chains including L2s (Arbitrum, Optimism, Base, etc.).
- The `request()` function with `useBlockHash = true` is a public, payable, permissionless entry point reachable by any user or contract.
- Any user or integrating contract that calls the legacy `request()` API with `useBlockHash = true` on an affected L2 is impacted — this includes users following older SDK examples or documentation that predate the `requestV2()` migration.
- Note: `requestV2()` / `requestWithCallback()` explicitly set `useBlockhash = false` and are not affected. [5](#0-4) 

---

### Recommendation

1. **Disallow `useBlockhash = true` on non-Ethereum chains**: Add a check in `requestHelper()` that reverts if `useBlockhash == true` on chains where `blockhash()` behavior is unreliable (or simply deprecate the flag entirely, since `requestV2()` already disables it).
2. **Alternatively**, replace `blockhash(req.blockNumber)` with `block.prevrandao` (EIP-4399) or another chain-agnostic entropy source that does not depend on the 256-block window.
3. **Document** that the legacy `request()` function with `useBlockHash = true` must not be used on L2 deployments.

---

### Proof of Concept

1. Deploy or interact with the Entropy contract on Arbitrum.
2. Call `request(provider, keccak256(abi.encodePacked(secret)), true)` with sufficient fee.
3. Wait for the transaction to confirm. Note `req.blockNumber` = current Arbitrum `block.number` = an L1 block number (e.g., 18,000,000).
4. Call `reveal(provider, sequenceNumber, secret, providerContribution)`.
5. Inside `revealHelper()`, `blockhash(18000000)` is called. Since Arbitrum's `blockhash()` indexes L2 blocks and L2 block 18,000,000 is outside the 256-block window (or the numbering space entirely), it returns `bytes32(0)`.
6. The transaction reverts with `BlockhashUnavailable`.
7. The user's fee is permanently lost; the request cannot be fulfilled. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L262-263)
```text
        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L322-326)
```text
    function request(
        address provider,
        bytes32 userCommitment,
        bool useBlockHash
    ) public payable override returns (uint64 assignedSequenceNumber) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L366-370)
```text
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L410-424)
```text
        blockHash = bytes32(uint256(0));
        if (req.useBlockhash) {
            bytes32 _blockHash = blockhash(req.blockNumber);

            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();

            blockHash = _blockHash;
        }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L55-63)
```text
        // The number of the block where this request was created.
        // Note that we're using a uint64 such that we have an additional space for an address and other fields in
        // this storage slot. Although block.number returns a uint256, 64 bits should be plenty to index all of the
        // blocks ever generated.
        uint64 blockNumber;
        // The address that requested this random number.
        address requester;
        // If true, incorporate the blockhash of blockNumber into the generated random value.
        bool useBlockhash;
```
