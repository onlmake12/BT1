### Title
Failed Transaction Not Checked Before Wormhole Event Parsing - (File: `contract_manager/src/node/utils/governance.ts`)

---

### Summary

`SubmittedWormholeMessage.fromTransactionSignature()` iterates over all instructions and log messages of a fetched Solana transaction without first checking whether the transaction succeeded (`txDetails?.meta?.err`). This is a direct analog to the Phoenix SDK bug: a failed transaction's inner instructions and log messages are still present in the RPC response and will be parsed as if the transaction succeeded.

---

### Finding Description

In `contract_manager/src/node/utils/governance.ts`, the static method `fromTransactionSignature` fetches a parsed transaction and immediately reads its `meta.logMessages` and `meta.innerInstructions` to extract a Wormhole `postMessage` emitter and sequence number: [1](#0-0) 

The function:
1. Reads `txDetails?.meta?.logMessages` to find the `"Sequence: "` log line and extract a sequence number.
2. Iterates over all outer and inner instructions to find a Wormhole `postMessage` instruction and extract the emitter.
3. **Never checks `txDetails?.meta?.err`.**

On Solana, a failed transaction still has its `meta.logMessages` and `meta.innerInstructions` populated in the RPC response — the runtime records execution up to the point of failure. Because Solana transactions are atomic, any state changes (including the Wormhole sequence counter increment) are rolled back on failure, but the logs and instruction data remain visible.

An attacker can craft a transaction that:
- Invokes the Wormhole program's `postMessage` via CPI from a custom on-chain program (so it appears in `innerInstructions`)
- Deliberately fails the outer transaction after the CPI (e.g., via a subsequent instruction that panics)

The failed transaction will contain a `"Sequence: N"` log line and a Wormhole `postMessage` inner instruction with an arbitrary emitter account. `fromTransactionSignature` will parse these and return a `SubmittedWormholeMessage` with attacker-influenced `emitter` and `sequenceNumber` values, even though no actual Wormhole message was ever posted. [2](#0-1) 

---

### Impact Explanation

The returned `SubmittedWormholeMessage` is used to call `fetchVaa()`, which queries the Wormhole Guardian API for a signed VAA at the extracted `(emitter, sequenceNumber)` pair: [3](#0-2) 

Since the transaction failed and no Wormhole message was actually posted, the Guardian API will return 404 for the fabricated sequence number. `fetchVaa()` will loop until timeout and throw `"VAA not found"`. This causes a **denial of service** for the governance pipeline: the operator's tooling stalls waiting for a VAA that will never arrive, blocking governance execution.

Additionally, if the attacker controls the emitter account passed in the inner instruction, they can cause `fetchVaa()` to query for a VAA under a completely different emitter address, potentially fetching an unrelated or attacker-controlled VAA from the Guardian API.

The `execute()` method of `WormholeMultisigProposal` calls `fromTransactionSignature` in a loop over all proposal execution signatures: [4](#0-3) 

A single poisoned signature in the loop causes the entire batch of governance messages to be lost or misrouted.

---

### Likelihood Explanation

The function is a **public static API** documented in the README as the canonical way to obtain a `SubmittedWormholeMessage` from a transaction signature. Any caller that passes an externally-supplied or incorrectly-obtained signature (e.g., from a UI, script, or API response) is vulnerable. The internal callers (`WormholeEmitter.sendMessage` and `WormholeMultisigProposal.execute`) both use `sendAndConfirm`/`sendTransactions` which throw on failure, so those paths are not directly exploitable — but the public API surface is. An unprivileged attacker can submit a crafted failing transaction on-chain and provide its signature to any consumer of this SDK method.

---

### Recommendation

Add an explicit check for `txDetails?.meta?.err` immediately after fetching the transaction, and throw (or return an error) if the transaction failed:

```typescript
const txDetails = await connection.getParsedTransaction(signature);
if (txDetails?.meta?.err) {
  throw new InvalidTransactionError(
    `Transaction ${signature} failed with error: ${JSON.stringify(txDetails.meta.err)}`
  );
}
```

This mirrors the fix described in the referenced Phoenix SDK patch (PR #50): skip processing of any transaction that did not complete successfully.

---

### Proof of Concept

1. Deploy a custom Solana program that CPIs into the Wormhole `postMessage` instruction with an arbitrary emitter account, then deliberately fails (e.g., returns an error after the CPI).
2. Submit this transaction to the target cluster. It will fail atomically, but the RPC will record the inner Wormhole instruction and the `"Sequence: N"` log line.
3. Obtain the transaction signature.
4. Call `SubmittedWormholeMessage.fromTransactionSignature(signature, cluster)`.
5. Observe that the function returns a `SubmittedWormholeMessage` with the attacker-chosen emitter and a sequence number from the failed transaction's logs.
6. Call `.fetchVaa()` on the result — it will loop until timeout and throw `"VAA not found"`, demonstrating the DoS. Alternatively, if the attacker's emitter matches a real emitter with a different sequence number, an unrelated VAA may be fetched.

### Citations

**File:** contract_manager/src/node/utils/governance.ts (L79-115)
```typescript
    const txDetails = await connection.getParsedTransaction(signature);
    const sequenceLogPrefix = "Sequence: ";
    const txLog = txDetails?.meta?.logMessages?.find((s) =>
      s.includes(sequenceLogPrefix),
    );

    const sequenceNumber = Number(
      txLog?.slice(
        Math.max(
          0,
          txLog.indexOf(sequenceLogPrefix) + sequenceLogPrefix.length,
        ),
      ),
    );

    const wormholeAddress = WORMHOLE_ADDRESS[cluster];
    if (!wormholeAddress) throw new Error(`Invalid cluster ${cluster}`);
    let emitter: PublicKey | undefined = undefined;

    let allInstructions: (ParsedInstruction | PartiallyDecodedInstruction)[] =
      txDetails?.transaction.message.instructions ?? [];
    if (txDetails?.meta?.innerInstructions)
      for (const instruction of txDetails.meta.innerInstructions) {
        allInstructions = [...allInstructions, ...instruction.instructions];
      }
    for (const instruction of allInstructions) {
      if (!instruction.programId.equals(wormholeAddress)) continue;
      // we assume RPC can not parse wormhole instructions and the type is not ParsedInstruction
      const wormholeInstruction = instruction as PartiallyDecodedInstruction;
      if (bs58.decode(wormholeInstruction.data)[0] !== 1) continue; // 1 is wormhole postMessage Instruction discriminator
      emitter = wormholeInstruction.accounts[2];
    }
    if (!emitter)
      throw new InvalidTransactionError(
        "Could not find wormhole postMessage instruction",
      );
    return new SubmittedWormholeMessage(emitter, sequenceNumber, cluster);
```

**File:** contract_manager/src/node/utils/governance.ts (L123-143)
```typescript
  async fetchVaa(waitingSeconds = 1): Promise<Buffer> {
    const rpcUrl = WORMHOLE_API_ENDPOINT[this.cluster];

    const startTime = Date.now();
    while (Date.now() - startTime < waitingSeconds * 1000) {
      const response = await fetch(
        `${rpcUrl}/v1/signed_vaa/1/${this.emitter.toBuffer().toString("hex")}/${
          this.sequenceNumber
        }`,
      );
      if (response.status === 404) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        continue;
      }
      const { vaaBytes } = (await response.json()) as {
        vaaBytes: Parameters<typeof Buffer.from>[0];
      };
      return Buffer.from(vaaBytes, "base64");
    }
    throw new Error("VAA not found, maybe too soon to fetch?");
  }
```

**File:** contract_manager/src/node/utils/governance.ts (L260-275)
```typescript
    const msgs: SubmittedWormholeMessage[] = [];
    for (const signature of signatures) {
      try {
        msgs.push(
          await SubmittedWormholeMessage.fromTransactionSignature(
            signature,
            this.cluster,
          ),
        );
      } catch (error: unknown) {
        if (!(error instanceof InvalidTransactionError)) throw error;
      }
    }
    if (msgs.length > 0) return msgs;
    throw new Error("No transactions with wormhole messages found");
  }
```
