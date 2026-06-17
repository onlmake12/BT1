### Title
Missing Signer-Publisher Authorization Check in `set_publisher_stake_account` Allows Any Caller to Redirect Publisher Self-Staking Rewards — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The `set_publisher_stake_account` instruction in the Integrity Pool program accepts an arbitrary `signer` with no on-chain constraint requiring that `signer == publisher`. Any unprivileged transaction sender can call this instruction for any registered publisher and redirect that publisher's designated self-staking account to an attacker-controlled account, causing the publisher's self-staking reward flow to be hijacked.

---

### Finding Description

The Integrity Pool program exposes a `set_publisher_stake_account` instruction whose account layout is:

```
signer          — isSigner: true  (no relations / has_one constraint)
publisher       — isSigner: false ("CHECK: The publisher will be checked against data in the pool_data")
pool_data       — writable
pool_config     — PDA
newStakeAccountPositionsOption   — optional
currentStakeAccountPositionsOption — optional
``` [1](#0-0) 

The `signer` account carries **no Anchor `relations`, `has_one`, or `constraint`** that ties it to `publisher`. The only check described for `publisher` is that it exists in `pool_data` — not that the signer is the publisher or any privileged authority. [2](#0-1) 

The same structure is confirmed in the canonical IDL used by the xc_admin multisig tooling, where the governance vault PDA is used as the signer — but nothing in the on-chain interface enforces that only governance (or the publisher itself) may sign. [3](#0-2) 

When `advance_delegation_record` is later called, the `publisher_stake_account_positions` and `publisher_stake_account_custody` accounts — which receive the publisher's self-staking bonus rewards — are resolved from whatever stake account is currently registered in `pool_data` for that publisher. [4](#0-3) 

---

### Impact Explanation

An attacker who controls any funded Solana wallet can:

1. Call `set_publisher_stake_account` with `publisher = victim_publisher`, `newStakeAccountPositionsOption = attacker_stake_account`, and any arbitrary `signer` key they control.
2. The pool data is updated so that `victim_publisher.stake_account = attacker_stake_account`.
3. On every subsequent `advance_delegation_record` call for the victim publisher, the self-staking reward tokens are transferred into the attacker's `stake_account_custody` PDA instead of the publisher's.
4. The attacker calls `withdraw` (via the staking program) to drain the accumulated PYTH tokens.

The publisher's self-staking rewards are permanently redirected until the publisher (or governance) calls `set_publisher_stake_account` again to restore the correct account — but by then tokens already distributed are lost.

**Impact class:** Theft of staking rewards from legitimate publishers. Severity scales with the number of publishers targeted and the reward rate.

---

### Likelihood Explanation

- The instruction is permissionless at the interface level; no privileged key is required.
- The attack requires only a funded Solana wallet and knowledge of a target publisher's public key (both are fully public on-chain).
- The attacker does not need to front-run, brute-force, or compromise any key.
- `advance_delegation_record` is called permissionlessly by the Fortuna keeper and by any staker claiming rewards, so the redirected reward flow activates automatically without further attacker action. [5](#0-4) 

---

### Recommendation

Add an explicit authorization constraint to the `set_publisher_stake_account` instruction requiring that the signer is either:

- The publisher key itself (`signer == publisher`), **or**
- The `pool_config.reward_program_authority` (governance).

In Anchor this would be expressed as a `constraint` or `has_one` on the account struct, which would also surface in the IDL and make the authorization auditable.

---

### Proof of Concept

```
// Attacker wallet: attacker_keypair (any funded Solana wallet)
// Victim: victim_publisher (any registered publisher in pool_data)
// Attacker's own stake account: attacker_stake_account

await integrityPoolProgram.methods
  .setPublisherStakeAccount()
  .accounts({
    signer: attacker_keypair.publicKey,          // arbitrary signer — no ownership check
    publisher: victim_publisher,                  // victim's publisher key
    poolData: POOL_DATA_ADDRESS,
    poolConfig: POOL_CONFIG_PDA,
    currentStakeAccountPositionsOption: victim_current_stake_account, // readable on-chain
    newStakeAccountPositionsOption: attacker_stake_account,           // attacker-controlled
  })
  .signers([attacker_keypair])
  .rpc();

// From this point forward, every advance_delegation_record call for victim_publisher
// deposits self-staking rewards into attacker_stake_account_custody.
// Attacker drains via the staking program's withdraw instruction.
``` [6](#0-5) 

> **Caveat:** The Rust source for the Integrity Pool program is not present in the indexed repository (only the compiled IDL is available). The finding is based on the absence of any Anchor account constraint linking `signer` to `publisher` in the IDL, which is the authoritative on-chain interface. If the program body contains an explicit `require!(ctx.accounts.signer.key() == ctx.accounts.publisher.key() || ...)` guard not reflected in the IDL, the vulnerability would not be exploitable. A full audit of the Rust source is recommended to confirm.

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L284-342)
```json
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "publisher_stake_account_positions",
          "optional": true
        },
        {
          "name": "publisher_stake_account_custody",
          "optional": true,
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "publisher_stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "name": "delegation_record",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  100, 101, 108, 101, 103, 97, 116, 105, 111, 110, 95, 114, 101,
                  99, 111, 114, 100
                ]
              },
              {
                "kind": "account",
                "path": "publisher"
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L721-760)
```json
    {
      "accounts": [
        {
          "name": "signer",
          "signer": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "new_stake_account_positions_option",
          "optional": true
        },
        {
          "name": "current_stake_account_positions_option",
          "optional": true
        }
      ],
      "args": [],
      "discriminator": [99, 46, 72, 132, 100, 235, 211, 117],
      "name": "set_publisher_stake_account"
```

**File:** governance/xc_admin/packages/xc_admin_common/src/multisig_transaction/idl/integrity-pool.json (L1-51)
```json
{
  "instructions": [
    {
      "accounts": [
        {
          "isMut": false,
          "isSigner": true,
          "name": "signer"
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "isMut": false,
          "isSigner": false,
          "name": "publisher"
        },
        {
          "isMut": true,
          "isSigner": false,
          "name": "poolData"
        },
        {
          "isMut": false,
          "isSigner": false,
          "name": "poolConfig",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "type": "string",
                "value": "pool_config"
              }
            ]
          }
        },
        {
          "isMut": false,
          "isOptional": true,
          "isSigner": false,
          "name": "newStakeAccountPositionsOption"
        },
        {
          "isMut": false,
          "isOptional": true,
          "isSigner": false,
          "name": "currentStakeAccountPositionsOption"
        }
      ],
      "args": [],
      "name": "setPublisherStakeAccount"
```

**File:** governance/xc_admin/packages/xc_admin_cli/src/index.ts (L1089-1101)
```typescript
    const setPublisherStakeAccountInstruction =
      await integrityPoolProgram.methods
        .setPublisherStakeAccount?.()
        .accounts({
          currentStakeAccountPositionsOption: INTEGRITY_POOL_PROGRAM_ID, // This corresponds to `None` for optional accounts in Anchor
          newStakeAccountPositionsOption,
          poolData: new PublicKey(
            "poo1zPoi5xrNzi4yk4i23oWcJrNNkDYAniBCewJY8kb",
          ),
          publisher: new PublicKey(options.publisher),
          signer: await vault.getVaultAuthorityPDA(targetCluster),
        })
        .instruction();
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L739-756)
```typescript
    const advanceDelegationRecordInstructions = await Promise.all(
      filteredPublishers.map(({ pubkey, stakeAccount }) =>
        this.integrityPoolProgram.methods
          .advanceDelegationRecord()
          .accountsPartial({
            payer: payer ?? this.wallet.publicKey,
            publisher: pubkey,
            publisherStakeAccountCustody: stakeAccount
              ? getStakeAccountCustodyAddress(stakeAccount)
              : null, // eslint-disable-line unicorn/no-null
            publisherStakeAccountPositions: stakeAccount,
            stakeAccountCustody: getStakeAccountCustodyAddress(
              stakeAccountPositions,
            ),
            stakeAccountPositions,
          })
          .instruction(),
      ),
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L827-844)
```typescript
  async setPublisherStakeAccount(
    publisher: PublicKey,
    stakeAccountPositions: PublicKey,
    newStakeAccountPositions: PublicKey | undefined,
  ) {
    const instruction = await this.integrityPoolProgram.methods
      .setPublisherStakeAccount()
      .accounts({
        currentStakeAccountPositionsOption: stakeAccountPositions,
        // eslint-disable-next-line unicorn/no-null
        newStakeAccountPositionsOption: newStakeAccountPositions ?? null,
        publisher,
      })
      .instruction();

    await sendTransaction([instruction], this.connection, this.wallet);
    return;
  }
```
