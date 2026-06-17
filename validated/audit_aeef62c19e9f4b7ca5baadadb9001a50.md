### Title
Trustless `create_stake_account` Allows Frontrunning to Hijack Stake Account Ownership Before Token Deposit - (`governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The Pyth staking program's `create_stake_account` instruction is explicitly labeled "Trustless" and accepts an arbitrary `owner` pubkey as an instruction argument with no verification that the caller is the intended owner. Because `stake_account_positions` is not required to be a signer, any party who observes an uninitialized `stake_account_positions` account on-chain can race to call `create_stake_account` first and set themselves as the owner — directly analogous to the deposit-contract frontrunning described in the reference report.

---

### Finding Description

The `create_stake_account` instruction in the Pyth staking program has the following account layout (from the IDL):

- `payer` — signer, writable (pays rent)
- `stake_account_positions` — **writable, NOT a signer, no ownership constraint**
- `stake_account_metadata` — PDA derived from `stake_account_positions`, initialized here
- `owner` — **instruction argument of type `pubkey`, not verified against any signer**

The program documentation explicitly states:

> "Trustless instruction that creates a stake account for a user. The main account i.e. the position accounts needs to be initialized outside of the program, otherwise we run into stack limits."

Because `stake_account_positions` must be created in a prior step (outside the program), and because `create_stake_account` places no constraint linking the `payer` to the `owner` argument or to the `stake_account_positions` account, any third party can call `create_stake_account` on an existing uninitialized `stake_account_positions` account and supply an arbitrary `owner`.

The standard SDK flow in `createStakeAccountAndDeposit` bundles `SystemProgram.createAccountWithSeed` and `create_stake_account` in the same atomic transaction, which prevents frontrunning in that specific path. However:

1. The program itself enforces no such atomicity requirement.
2. Any client that separates account creation from `create_stake_account` (e.g., a custom integration, a governance-driven batch, or a retry after a partial failure) is vulnerable.
3. The existence of `recover_account` — which exists to fix cases where the wrong owner was set — confirms the designers recognized ownership can be set incorrectly, but the frontrunning vector was not closed at the program level.

---

### Impact Explanation

If an attacker frontruns `create_stake_account`:

1. `stake_account_metadata` is initialized with the attacker's address as `owner`.
2. The victim's subsequent `create_stake_account` call fails (Anchor `init` constraint rejects re-initialization).
3. The victim, unaware, calls `depositTokensToStakeAccountCustody` — a plain SPL token transfer to the PDA `custody` address derived from `stake_account_positions` — which succeeds regardless of who owns the metadata.
4. The attacker, now the recorded `owner`, calls `withdraw_stake` (which requires `owner` to sign via `relations: ["stake_account_metadata"]`) and drains the deposited PYTH tokens.

Impact: **direct theft of staked PYTH tokens** from any user whose account creation and initialization are not atomic.

---

### Likelihood Explanation

The standard SDK path (`createStakeAccountAndDeposit`) is atomic and not directly exploitable. However:

- The program-level interface is permissionless and the vulnerability is structural.
- Any non-SDK client, governance script, or retry logic that separates the two steps is immediately exploitable.
- A malicious Solana validator can reorder transactions within a block; if a user submits account creation and `create_stake_account` as two separate transactions (even in the same block), the validator can insert the attacker's `create_stake_account` between them.
- The `recover_account` instruction requires `governance_authority` to intervene — meaning victims have no self-service remedy.

Likelihood: **Medium** — not exploitable via the reference SDK path, but exploitable at the program level by any party who can observe an uninitialized `stake_account_positions` account before its metadata is set.

---

### Recommendation

Add a constraint in `create_stake_account` that verifies the `payer` matches the `owner` argument:

```rust
require!(ctx.accounts.payer.key() == owner, StakingError::OwnerMismatch);
```

Alternatively, derive `owner` from the `payer` signer rather than accepting it as a free argument, eliminating the frontrunning surface entirely. The "trustless" label should be removed or scoped to mean "anyone can pay rent" rather than "anyone can set the owner."

---

### Proof of Concept

1. Alice submits transaction T1: `SystemProgram.createAccountWithSeed(basePubkey=Alice, seed=nonce, programId=staking)` → creates `stake_account_positions` at address `P`.

2. Bob (attacker) observes `P` on-chain (T1 confirmed, T2 not yet submitted).

3. Bob submits transaction T2: `create_stake_account(owner=Bob, lock=fullyVested)` with `stake_account_positions=P`.
   - `stake_account_metadata[P]` is initialized with `owner = Bob`.

4. Alice submits transaction T3: `create_stake_account(owner=Alice, lock=fullyVested)` with `stake_account_positions=P`.
   - **Fails**: Anchor `init` rejects re-initialization of `stake_account_metadata[P]`.

5. Alice, confused, calls `depositTokensToStakeAccountCustody(stakeAccountPositions=P, amount=X)`.
   - Succeeds: plain SPL transfer to `custody[P]` PDA, no ownership check.

6. Bob calls `withdraw_stake(amount=X, stakeAccountPositions=P)`.
   - Succeeds: Bob is the recorded `owner` in `stake_account_metadata[P]`, satisfying the `relations` constraint.
   - Bob receives Alice's PYTH tokens.

**Key files:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L660-772)
```json
    {
      "accounts": [
        {
          "name": "payer",
          "signer": true,
          "writable": true
        },
        {
          "name": "stake_account_positions",
          "writable": true
        },
        {
          "name": "stake_account_metadata",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 116, 97, 107, 101, 95, 109, 101, 116, 97, 100, 97, 116,
                  97
                ]
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
          "name": "stake_account_custody",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
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
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "custody_authority",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [97, 117, 116, 104, 111, 114, 105, 116, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          }
        },
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "pyth_token_mint",
          "relations": ["config"]
        },
        {
          "address": "SysvarRent111111111111111111111111111111111",
          "name": "rent"
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
      "args": [
        {
          "name": "owner",
          "type": "pubkey"
        },
        {
          "name": "lock",
          "type": {
            "defined": {
              "name": "VestingSchedule"
            }
          }
        }
      ],
      "discriminator": [105, 24, 131, 19, 201, 250, 157, 73],
      "docs": [
        "Trustless instruction that creates a stake account for a user",
        "The main account i.e. the position accounts needs to be initialized outside of the program",
        "otherwise we run into stack limits"
      ],
      "name": "create_stake_account"
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1089-1158)
```json
    {
      "accounts": [
        {
          "name": "governance_authority",
          "relations": ["config"],
          "signer": true
        },
        {
          "name": "owner",
          "relations": ["stake_account_metadata"]
        },
        {
          "name": "stake_account_positions",
          "writable": true
        },
        {
          "name": "stake_account_metadata",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 116, 97, 107, 101, 95, 109, 101, 116, 97, 100, 97, 116,
                  97
                ]
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
          "name": "voter_record",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  118, 111, 116, 101, 114, 95, 119, 101, 105, 103, 104, 116
                ]
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
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        }
      ],
      "args": [],
      "discriminator": [240, 223, 246, 118, 26, 121, 34, 128],
      "docs": [
        "Recovers a user's `stake account` ownership by transferring ownership\n     * from a token account to the `owner` of that token account.\n     *\n     * This functionality addresses the scenario where a user mistakenly\n     * created a stake account using their token account address as the owner."
      ],
      "name": "recover_account"
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L458-535)
```typescript
  public async createStakeAccountAndDeposit(amount: bigint) {
    const globalConfig = await this.getGlobalConfig();

    const senderTokenAccount = getAssociatedTokenAddressSync(
      globalConfig.pythTokenMint,
      this.wallet.publicKey,
      true,
    );

    const nonce = crypto.randomBytes(16).toString("hex");
    const stakeAccountPositions = await PublicKey.createWithSeed(
      this.wallet.publicKey,
      nonce,
      this.stakingProgram.programId,
    );

    const minimumBalance =
      await this.stakingProgram.provider.connection.getMinimumBalanceForRentExemption(
        POSITIONS_ACCOUNT_SIZE,
      );

    const instructions = [];

    instructions.push(
      SystemProgram.createAccountWithSeed({
        basePubkey: this.wallet.publicKey,
        fromPubkey: this.wallet.publicKey,
        lamports: minimumBalance,
        newAccountPubkey: stakeAccountPositions,
        programId: this.stakingProgram.programId,
        seed: nonce,
        space: POSITIONS_ACCOUNT_SIZE,
      }),
      await this.stakingProgram.methods
        .createStakeAccount(this.wallet.publicKey, { fullyVested: {} })
        .accounts({
          stakeAccountPositions,
        })
        .instruction(),
      await this.stakingProgram.methods
        .createVoterRecord()
        .accounts({
          stakeAccountPositions,
        })
        .instruction(),
    );

    if (!(await this.hasGovernanceRecord(globalConfig))) {
      await withCreateTokenOwnerRecord(
        instructions,
        GOVERNANCE_ADDRESS,
        PROGRAM_VERSION_V2,
        globalConfig.pythGovernanceRealm,
        this.wallet.publicKey,
        globalConfig.pythTokenMint,
        this.wallet.publicKey,
      );
    }

    instructions.push(
      await this.stakingProgram.methods
        .joinDaoLlc(globalConfig.agreementHash)
        .accounts({
          stakeAccountPositions,
        })
        .instruction(),
      createTransferInstruction(
        senderTokenAccount,
        getStakeAccountCustodyAddress(stakeAccountPositions),
        this.wallet.publicKey,
        amount,
      ),
    );

    await sendTransaction(instructions, this.connection, this.wallet);

    return stakeAccountPositions;
  }
```
