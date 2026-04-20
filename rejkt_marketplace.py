"""
REJKT Marketplace Contract
==========================
Handles listing (swap), buying (collect), and cancelling sales of FA2 tokens.

Features:
- Anyone can list (swap) their FA2 tokens for sale at a fixed price
- Anyone can buy (collect) a listed token by paying the price
- Seller can cancel their own listing at any time
- Optional platform fee (set at origination, immutable — deploy a new contract to change)
- Royalty pass-through: reads royalty info from the FA2 contract via onchain view
  and distributes to royalty recipients before paying the seller
- Works with any FA2 fungible contract that exposes a `get_royalties` onchain view
  in the format: sp.map[sp.address, sp.nat]  (address → share out of 1000)

Instructions:
1. Paste this file into the SmartPy IDE at https://smartpy.io/ide
2. Set FA2_CONTRACT_ADDRESS to your deployed FA2 contract address
3. Set ADMIN_ADDRESS, FEE_PERCENT, FEE_RECIPIENT before deploying
4. Run tests, deploy to Ghostnet first, then Mainnet

Important:
- FEE_PERCENT and FEE_RECIPIENT are set at origination and CANNOT be changed.
  To update them, deploy a new marketplace contract.
- The FA2 contract must have approved this marketplace as an operator before
  a swap can be executed. The swap entrypoint handles this via update_operators.
- Swap IDs are sequential starting at 0 and are never reused.
"""

import smartpy as sp

# ── Constants (edit before deploying) ─────────────────────────────────────────

ADMIN_ADDRESS       = "tz1YourWalletAddressHere"   # Your Tezos wallet
FA2_CONTRACT_ADDRESS = "KT1YourFA2ContractHere"     # Deployed FA2 contract
FEE_PERCENT         = sp.nat(0)                     # 0 = no fee. E.g. 25 = 2.5%
FEE_RECIPIENT       = "tz1YourWalletAddressHere"    # Where platform fees go

# ─────────────────────────────────────────────────────────────────────────────

@sp.module
def marketplace_module():

    # ── Storage types ─────────────────────────────────────────────────────────

    swap_type: type = sp.record(
        seller      = sp.address,   # who listed the token
        fa2         = sp.address,   # FA2 contract address
        token_id    = sp.nat,       # FA2 token ID
        amount      = sp.nat,       # number of editions listed
        price       = sp.mutez,     # price per edition
    )

    # ── FA2 interface types (for cross-contract calls) ────────────────────────

    # transfer entrypoint parameter type (FA2 standard)
    fa2_transfer_tx: type = sp.record(
        to_      = sp.address,
        token_id = sp.nat,
        amount   = sp.nat,
    ).layout(("to_", ("token_id", "amount")))

    fa2_transfer_batch: type = sp.record(
        from_ = sp.address,
        txs   = sp.list[fa2_transfer_tx],
    ).layout(("from_", "txs"))

    # update_operators entrypoint parameter type (FA2 standard)
    fa2_operator_permission: type = sp.record(
        owner    = sp.address,
        operator = sp.address,
        token_id = sp.nat,
    ).layout(("owner", ("operator", "token_id")))

    fa2_update_operators_params: type = sp.list[sp.variant(
        add_operator    = fa2_operator_permission,
        remove_operator = fa2_operator_permission,
    )]

    # ── Contract ──────────────────────────────────────────────────────────────

    class REJKTMarketplace(sp.Contract):
        def __init__(
            self,
            admin_address,
            fa2_contract,
            fee_percent,
            fee_recipient,
        ):
            self.data.administrator  = sp.cast(admin_address,  sp.address)
            self.data.fa2_contract   = sp.cast(fa2_contract,   sp.address)
            self.data.fee_percent    = sp.cast(fee_percent,    sp.nat)
            self.data.fee_recipient  = sp.cast(fee_recipient,  sp.address)
            self.data.next_swap_id   = sp.nat(0)
            self.data.swaps          = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, swap_type]
            )

        # ── Swap (list for sale) ─────────────────────────────────────────

        @sp.entrypoint
        def swap(self, params):
            """
            List FA2 tokens for sale.

            The caller must own the tokens. This entrypoint:
              1. Adds this marketplace as an operator on the FA2 contract
                 (so it can transfer on the seller's behalf at collect time)
              2. Records the swap

            params:
              - token_id : sp.nat    — FA2 token ID to list
              - amount   : sp.nat    — number of editions to list (min 1)
              - price    : sp.mutez  — price per edition
            """
            assert params.amount >= sp.nat(1), "MIN_ONE_EDITION"
            assert params.price  >  sp.mutez(0), "PRICE_MUST_BE_POSITIVE"

            # Grant this marketplace operator rights on the FA2 so it can
            # transfer tokens from the seller's wallet when someone collects.
            fa2 = sp.contract(
                fa2_update_operators_params,
                self.data.fa2_contract,
                entrypoint="update_operators"
            ).unwrap_some("BAD_FA2_CONTRACT")

            sp.transfer(
                [sp.variant("add_operator", sp.record(
                    owner    = sp.sender,
                    operator = sp.self_address,
                    token_id = params.token_id,
                ))],
                sp.mutez(0),
                fa2
            )

            # Record the swap
            self.data.swaps[self.data.next_swap_id] = sp.record(
                seller   = sp.sender,
                fa2      = self.data.fa2_contract,
                token_id = params.token_id,
                amount   = params.amount,
                price    = params.price,
            )

            self.data.next_swap_id += 1

        # ── Collect (buy) ────────────────────────────────────────────────

        @sp.entrypoint
        def collect(self, params):
            """
            Buy editions from a swap.

            Distributes payment in this order:
              1. Platform fee (if fee_percent > 0) → fee_recipient
              2. Royalties → each royalty recipient pro-rata
              3. Remainder → seller

            After payment, transfers the tokens from seller to buyer via FA2.
            If all editions are sold, removes operator rights and deletes swap.

            params:
              - swap_id  : sp.nat  — ID of the swap to collect from
              - quantity : sp.nat  — number of editions to buy (min 1)
            """
            assert self.data.swaps.contains(params.swap_id), "UNKNOWN_SWAP"

            swap = self.data.swaps[params.swap_id]

            assert params.quantity >= sp.nat(1),         "MIN_ONE_EDITION"
            assert params.quantity <= swap.amount,        "EXCEEDS_AVAILABLE"
            assert sp.sender != swap.seller,              "CANNOT_BUY_OWN_SWAP"

            # Payment check
            total_price = sp.split_tokens(swap.price, params.quantity, sp.nat(1))
            assert sp.amount == total_price, "WRONG_AMOUNT"

            remaining = sp.local("remaining", sp.amount)

            # 1. Platform fee
            sp.if self.data.fee_percent > sp.nat(0):
                fee = sp.split_tokens(sp.amount, self.data.fee_percent, sp.nat(1000))
                sp.if fee > sp.mutez(0):
                    sp.send(self.data.fee_recipient, fee)
                    remaining.value = sp.as_nat(
                        sp.amount - fee,
                        error="FEE_EXCEEDS_AMOUNT"
                    ) * sp.mutez(1)

            # 2. Royalties — read from FA2 onchain view
            #    get_royalties returns sp.map[sp.address, sp.nat]
            #    where each value is a share out of 1000
            royalty_map = sp.View(swap.fa2, "get_royalties")(swap.token_id)
            sp.cast(royalty_map, sp.map[sp.address, sp.nat])

            for recipient, share in royalty_map.items():
                sp.if share > sp.nat(0):
                    royalty_amount = sp.split_tokens(sp.amount, share, sp.nat(1000))
                    sp.if royalty_amount > sp.mutez(0):
                        # Only pay royalty if it doesn't exceed what's left
                        sp.if royalty_amount <= remaining.value:
                            sp.send(recipient, royalty_amount)
                            remaining.value = sp.as_nat(
                                remaining.value - royalty_amount,
                                error="ROYALTY_EXCEEDS_REMAINING"
                            ) * sp.mutez(1)

            # 3. Remainder to seller
            sp.if remaining.value > sp.mutez(0):
                sp.send(swap.seller, remaining.value)

            # Transfer tokens from seller to buyer via FA2
            fa2_transfer = sp.contract(
                sp.list[fa2_transfer_batch],
                swap.fa2,
                entrypoint="transfer"
            ).unwrap_some("BAD_FA2_CONTRACT")

            sp.transfer(
                [sp.record(
                    from_ = swap.seller,
                    txs   = [sp.record(
                        to_      = sp.sender,
                        token_id = swap.token_id,
                        amount   = params.quantity,
                    )]
                )],
                sp.mutez(0),
                fa2_transfer
            )

            # Update or close the swap
            new_amount = sp.as_nat(swap.amount - params.quantity, error="UNDERFLOW")
            sp.if new_amount == sp.nat(0):
                # All editions sold — revoke operator rights and delete swap
                fa2_ops = sp.contract(
                    fa2_update_operators_params,
                    swap.fa2,
                    entrypoint="update_operators"
                ).unwrap_some("BAD_FA2_CONTRACT")

                sp.transfer(
                    [sp.variant("remove_operator", sp.record(
                        owner    = swap.seller,
                        operator = sp.self_address,
                        token_id = swap.token_id,
                    ))],
                    sp.mutez(0),
                    fa2_ops
                )

                del self.data.swaps[params.swap_id]
            sp.else:
                self.data.swaps[params.swap_id].amount = new_amount

        # ── Cancel swap ──────────────────────────────────────────────────

        @sp.entrypoint
        def cancel_swap(self, swap_id):
            """
            Cancel a swap and revoke operator rights.
            Only the original seller can cancel.

            swap_id: sp.nat
            """
            assert self.data.swaps.contains(swap_id), "UNKNOWN_SWAP"

            swap = self.data.swaps[swap_id]

            assert sp.sender == swap.seller, "NOT_SELLER"

            # Revoke operator rights on FA2
            fa2_ops = sp.contract(
                fa2_update_operators_params,
                swap.fa2,
                entrypoint="update_operators"
            ).unwrap_some("BAD_FA2_CONTRACT")

            sp.transfer(
                [sp.variant("remove_operator", sp.record(
                    owner    = swap.seller,
                    operator = sp.self_address,
                    token_id = swap.token_id,
                ))],
                sp.mutez(0),
                fa2_ops
            )

            del self.data.swaps[swap_id]

        # ── Admin: update FA2 contract address ───────────────────────────

        @sp.entrypoint
        def set_fa2_contract(self, new_fa2):
            """
            Admin can point the marketplace at a different FA2 contract.
            Useful if you redeploy the FA2 (e.g. after a Ghostnet→Mainnet move).
            Does not affect existing swaps.
            """
            assert sp.sender == self.data.administrator, "NOT_ADMIN"
            self.data.fa2_contract = new_fa2

        # ── View: get swap ───────────────────────────────────────────────

        @sp.onchain_view()
        def get_swap(self, swap_id):
            """Returns the swap record for a given swap_id."""
            assert self.data.swaps.contains(swap_id), "UNKNOWN_SWAP"
            return self.data.swaps[swap_id]


# ── Tests ─────────────────────────────────────────────────────────────────────

@sp.add_test()
def test():
    scenario = sp.test_scenario("REJKT Marketplace Tests", marketplace_module)
    scenario.h1("REJKT Marketplace Contract Tests")

    # Accounts
    admin  = sp.test_account("Admin")
    alice  = sp.test_account("Alice")   # seller
    bob    = sp.test_account("Bob")     # buyer
    carol  = sp.test_account("Carol")   # royalty recipient

    # ── Deploy a stub FA2 so we can test the marketplace in isolation ─────────
    #
    # In the real integration the marketplace calls:
    #   - fa2.update_operators  (add/remove operator)
    #   - fa2.transfer          (move tokens)
    #   - fa2.get_royalties     (onchain view)
    #
    # We stub all three using a minimal helper contract.

    @sp.module
    def stub_module():

        class StubFA2(sp.Contract):
            """
            Minimal FA2 stub for marketplace testing.
            Tracks operator grants and transfers so we can assert on them.
            """
            def __init__(self, royalty_recipient, royalty_share):
                # ledger: (owner, token_id) → amount
                self.data.ledger = sp.cast(
                    sp.big_map(),
                    sp.big_map[sp.pair[sp.address, sp.nat], sp.nat]
                )
                # operators: (owner, operator, token_id) → unit
                self.data.operators = sp.cast(
                    sp.big_map(),
                    sp.big_map[sp.record(
                        owner    = sp.address,
                        operator = sp.address,
                        token_id = sp.nat,
                    ), sp.unit]
                )
                # Simple single royalty recipient for tests
                self.data.royalty_recipient = sp.cast(royalty_recipient, sp.address)
                self.data.royalty_share     = sp.cast(royalty_share,     sp.nat)

            @sp.entrypoint
            def update_operators(self, batch):
                for action in batch:
                    match action:
                        case add_operator(op):
                            self.data.operators[op] = ()
                        case remove_operator(op):
                            del self.data.operators[op]

            @sp.entrypoint
            def transfer(self, batch):
                for transfer in batch:
                    for tx in transfer.txs:
                        from_key = sp.pair(transfer.from_, tx.token_id)
                        bal = self.data.ledger.get(from_key, default=sp.nat(0))
                        assert bal >= tx.amount, "FA2_INSUFFICIENT_BALANCE"
                        self.data.ledger[from_key] = sp.as_nat(bal - tx.amount)
                        to_key = sp.pair(tx.to_, tx.token_id)
                        self.data.ledger[to_key] = (
                            self.data.ledger.get(to_key, default=sp.nat(0)) + tx.amount
                        )

            @sp.onchain_view()
            def get_royalties(self, token_id):
                sp.cast(token_id, sp.nat)
                return {self.data.royalty_recipient: self.data.royalty_share}

    scenario.register(stub_module)

    # ── Deploy stub FA2 ───────────────────────────────────────────────────────

    scenario.h2("1. Deploy stub FA2")
    fa2 = stub_module.StubFA2(
        royalty_recipient = carol.address,
        royalty_share     = sp.nat(100),  # 10% royalty
    )
    scenario += fa2

    # Seed Alice with 10 editions of token 0
    fa2.transfer(
        [sp.record(
            from_ = admin.address,
            txs   = [sp.record(to_=alice.address, token_id=sp.nat(0), amount=sp.nat(10))]
        )],
        _sender = admin
    )
    # (stub doesn't check admin — just seeds the ledger directly)
    scenario.verify(
        fa2.data.ledger[sp.pair(alice.address, sp.nat(0))] == sp.nat(10)
    )

    # ── Deploy marketplace (no fee) ───────────────────────────────────────────

    scenario.h2("2. Deploy marketplace (0% fee)")
    market = marketplace_module.REJKTMarketplace(
        admin_address = admin.address,
        fa2_contract  = fa2.address,
        fee_percent   = sp.nat(0),
        fee_recipient = admin.address,
    )
    scenario += market

    scenario.verify(market.data.fee_percent  == sp.nat(0))
    scenario.verify(market.data.next_swap_id == sp.nat(0))

    # ── Swap: Alice lists 5 editions at 2 tez each ────────────────────────────

    scenario.h2("3. Alice lists 5 editions of token 0 at 2 tez each")
    market.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(5), price=sp.tez(2)),
        _sender = alice
    )
    scenario.verify(market.data.next_swap_id == sp.nat(1))
    scenario.verify(market.data.swaps[sp.nat(0)].amount == sp.nat(5))
    scenario.verify(market.data.swaps[sp.nat(0)].price  == sp.tez(2))
    # Marketplace should now be an operator for alice/token0
    scenario.verify(fa2.data.operators.contains(sp.record(
        owner    = alice.address,
        operator = market.address,
        token_id = sp.nat(0),
    )))

    # ── Collect: Bob buys 2 editions ──────────────────────────────────────────

    scenario.h2("4. Bob buys 2 editions (4 tez — 10% royalty to Carol)")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(2)),
        _sender = bob,
        _amount = sp.tez(4)
    )
    # Bob should have 2 editions
    scenario.verify(
        fa2.data.ledger[sp.pair(bob.address, sp.nat(0))] == sp.nat(2)
    )
    # Alice should have 8 left (10 - 2 sold from swap)
    # (swap still open with 3 remaining)
    scenario.verify(market.data.swaps[sp.nat(0)].amount == sp.nat(3))

    # ── Wrong amount rejected ─────────────────────────────────────────────────

    scenario.h2("5. Bob underpays — should fail")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(1)),
        _sender = bob,
        _amount = sp.tez(1),  # should be 2 tez
        _valid  = False
    )

    # ── Seller cannot buy own swap ────────────────────────────────────────────

    scenario.h2("6. Alice tries to buy her own swap — should fail")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(1)),
        _sender = alice,
        _amount = sp.tez(2),
        _valid  = False
    )

    # ── Exceed available editions ─────────────────────────────────────────────

    scenario.h2("7. Bob tries to buy more than available — should fail")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(10)),
        _sender = bob,
        _amount = sp.tez(20),
        _valid  = False
    )

    # ── Cancel swap ───────────────────────────────────────────────────────────

    scenario.h2("8. Alice cancels swap 0")
    market.cancel_swap(sp.nat(0), _sender=alice)
    scenario.verify(~market.data.swaps.contains(sp.nat(0)))
    # Operator rights should be revoked
    scenario.verify(~fa2.data.operators.contains(sp.record(
        owner    = alice.address,
        operator = market.address,
        token_id = sp.nat(0),
    )))

    # ── Non-seller cannot cancel ──────────────────────────────────────────────

    scenario.h2("9. Alice re-lists, Bob tries to cancel — should fail")
    market.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(3), price=sp.tez(1)),
        _sender = alice
    )
    scenario.verify(market.data.next_swap_id == sp.nat(2))
    market.cancel_swap(sp.nat(1), _sender=bob, _valid=False)

    # ── Collect last edition closes swap automatically ────────────────────────

    scenario.h2("10. Bob buys all 3 remaining — swap should auto-close")
    market.collect(
        sp.record(swap_id=sp.nat(1), quantity=sp.nat(3)),
        _sender = bob,
        _amount = sp.tez(3)
    )
    scenario.verify(~market.data.swaps.contains(sp.nat(1)))
    # Operator rights auto-revoked
    scenario.verify(~fa2.data.operators.contains(sp.record(
        owner    = alice.address,
        operator = market.address,
        token_id = sp.nat(0),
    )))

    # ── Marketplace with fee ──────────────────────────────────────────────────

    scenario.h2("11. Deploy marketplace with 2.5% fee")
    market_fee = marketplace_module.REJKTMarketplace(
        admin_address = admin.address,
        fa2_contract  = fa2.address,
        fee_percent   = sp.nat(25),    # 25/1000 = 2.5%
        fee_recipient = admin.address,
    )
    scenario += market_fee
    scenario.verify(market_fee.data.fee_percent == sp.nat(25))

    # Alice lists 2 editions at 10 tez each on the fee marketplace
    market_fee.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(2), price=sp.tez(10)),
        _sender = alice
    )
    # Bob collects 1 — 10 tez total:
    #   fee    = 10 * 25 / 1000 = 0.25 tez  → admin
    #   royalty= 10 * 100/ 1000 = 1.0  tez  → carol
    #   seller = 10 - 0.25 - 1.0       = 8.75 tez → alice
    market_fee.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(1)),
        _sender = bob,
        _amount = sp.tez(10)
    )
    scenario.verify(
        fa2.data.ledger[sp.pair(bob.address, sp.nat(0))] == sp.nat(6)  # 2+1+3 from earlier
    )

    # ── Admin: update FA2 contract ────────────────────────────────────────────

    scenario.h2("12. Admin updates FA2 contract address")
    market.set_fa2_contract(sp.address("KT1Fake"), _sender=admin)
    scenario.verify(market.data.fa2_contract == sp.address("KT1Fake"))

    # Non-admin cannot update
    market.set_fa2_contract(fa2.address, _sender=alice, _valid=False)

    # ── Onchain view: get_swap ────────────────────────────────────────────────

    scenario.h2("13. get_swap view returns correct data")
    scenario.verify(market_fee.get_swap(sp.nat(0)).price == sp.tez(10))

    # Unknown swap
    scenario.verify_equal(
        market_fee.swaps.contains(sp.nat(99)), False
    )

    scenario.h1("All tests passed.")


# ── Compilation target ────────────────────────────────────────────────────────

sp.add_compilation_target(
    "REJKT_Marketplace",
    marketplace_module.REJKTMarketplace(
        admin_address = sp.address(ADMIN_ADDRESS),
        fa2_contract  = sp.address(FA2_CONTRACT_ADDRESS),
        fee_percent   = FEE_PERCENT,
        fee_recipient = sp.address(FEE_RECIPIENT),
    )
)
