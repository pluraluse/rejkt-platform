"""
REJKT Marketplace Contract
==========================
Handles listing (swap), buying (collect), and cancelling sales of FA2 tokens.

Features:
- Anyone can list (swap) their FA2 tokens for sale at a fixed price
- Anyone can buy (collect) a listed token by paying the price
- Seller can cancel their own listing at any time
- Optional platform fee (set at origination, immutable)
- Royalty pass-through via the FA2 contract's get_royalties onchain view

Instructions:
1. Paste this file into the SmartPy IDE at https://smartpy.io/ide
2. Set FA2_CONTRACT_ADDRESS to your deployed FA2 contract address
3. Set ADMIN_ADDRESS, FEE_PERCENT, FEE_RECIPIENT before deploying
4. Run tests, deploy to Ghostnet first, then Mainnet

Important:
- FEE_PERCENT and FEE_RECIPIENT are set at origination and CANNOT be changed.
  To update them, deploy a new marketplace contract.
- Swap IDs are sequential starting at 0 and are never reused.
- The marketplace adds itself as an FA2 operator when a swap is created,
  and removes itself when a swap is fully sold or cancelled.
"""

import smartpy as sp

# ── Constants (edit before deploying) ─────────────────────────────────────────

ADMIN_ADDRESS        = "tz1YourWalletAddressHere"
FA2_CONTRACT_ADDRESS = "KT1YourFA2ContractHere"
FEE_PERCENT          = sp.nat(0)                  # 0 = no fee. 25 = 2.5%
FEE_RECIPIENT        = "tz1YourWalletAddressHere"

# ─────────────────────────────────────────────────────────────────────────────

@sp.module
def marketplace_module():

    # ── FA2 interface types ───────────────────────────────────────────────────
    # Defined here (not imported from fa2_lib) so the marketplace is
    # self-contained and can work with any compliant FA2 contract.

    fa2_operator_permission: type = sp.record(
        owner    = sp.address,
        operator = sp.address,
        token_id = sp.nat,
    ).layout(("owner", ("operator", "token_id")))

    fa2_update_operators_params: type = sp.list[sp.variant(
        add_operator    = fa2_operator_permission,
        remove_operator = fa2_operator_permission,
    )]

    fa2_transfer_tx: type = sp.record(
        to_      = sp.address,
        token_id = sp.nat,
        amount   = sp.nat,
    ).layout(("to_", ("token_id", "amount")))

    fa2_transfer_batch: type = sp.record(
        from_ = sp.address,
        txs   = sp.list[fa2_transfer_tx],
    ).layout(("from_", "txs"))

    # ── Swap record ───────────────────────────────────────────────────────────

    swap_type: type = sp.record(
        seller   = sp.address,
        fa2      = sp.address,
        token_id = sp.nat,
        amount   = sp.nat,
        price    = sp.mutez,
    )

    # ── Stub FA2 (for tests only) ─────────────────────────────────────────────
    # A minimal FA2-compatible contract that implements the three entrypoints
    # the marketplace calls and the get_royalties onchain view.

    class StubFA2(sp.Contract):
        def __init__(self, royalty_recipient, royalty_share):
            self.data.ledger = sp.cast(
                sp.big_map(),
                sp.big_map[sp.pair[sp.address, sp.nat], sp.nat]
            )
            self.data.operators = sp.cast(
                sp.big_map(),
                sp.big_map[fa2_operator_permission, sp.unit]
            )
            self.data.royalty_recipient = sp.cast(royalty_recipient, sp.address)
            self.data.royalty_share     = sp.cast(royalty_share, sp.nat)

        @sp.entrypoint
        def set_balance(self, params):
            """Test helper: seed a balance directly."""
            self.data.ledger[sp.pair(params.owner, params.token_id)] = params.amount

        @sp.entrypoint
        def update_operators(self, batch):
            sp.cast(batch, fa2_update_operators_params)
            for action in batch:
                sp.if action.is_variant("add_operator"):
                    op = action.open_variant("add_operator")
                    self.data.operators[op] = sp.unit
                sp.if action.is_variant("remove_operator"):
                    op = action.open_variant("remove_operator")
                    del self.data.operators[op]

        @sp.entrypoint
        def transfer(self, batch):
            sp.cast(batch, sp.list[fa2_transfer_batch])
            for xfer in batch:
                for tx in xfer.txs:
                    from_key = sp.pair(xfer.from_, tx.token_id)
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
            return sp.map({self.data.royalty_recipient: self.data.royalty_share})

    # ── Marketplace ───────────────────────────────────────────────────────────

    class REJKTMarketplace(sp.Contract):
        def __init__(self, admin_address, fa2_contract, fee_percent, fee_recipient):
            self.data.administrator = sp.cast(admin_address,  sp.address)
            self.data.fa2_contract  = sp.cast(fa2_contract,   sp.address)
            self.data.fee_percent   = sp.cast(fee_percent,    sp.nat)
            self.data.fee_recipient = sp.cast(fee_recipient,  sp.address)
            self.data.next_swap_id  = sp.nat(0)
            self.data.swaps         = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, swap_type]
            )

        # ── private helpers ───────────────────────────────────────────────

        @sp.private()
        def _add_operator(self, params):
            """Grant marketplace operator rights on the FA2."""
            sp.cast(params, sp.record(fa2=sp.address, owner=sp.address, token_id=sp.nat))
            fa2_ops = sp.contract(
                fa2_update_operators_params,
                params.fa2,
                entrypoint="update_operators"
            ).unwrap_some("BAD_FA2_CONTRACT")
            sp.transfer(
                [sp.variant("add_operator", sp.record(
                    owner    = params.owner,
                    operator = sp.self_address,
                    token_id = params.token_id,
                ))],
                sp.mutez(0),
                fa2_ops
            )

        @sp.private()
        def _remove_operator(self, params):
            """Revoke marketplace operator rights on the FA2."""
            sp.cast(params, sp.record(fa2=sp.address, owner=sp.address, token_id=sp.nat))
            fa2_ops = sp.contract(
                fa2_update_operators_params,
                params.fa2,
                entrypoint="update_operators"
            ).unwrap_some("BAD_FA2_CONTRACT")
            sp.transfer(
                [sp.variant("remove_operator", sp.record(
                    owner    = params.owner,
                    operator = sp.self_address,
                    token_id = params.token_id,
                ))],
                sp.mutez(0),
                fa2_ops
            )

        # ── swap (list for sale) ──────────────────────────────────────────

        @sp.entrypoint
        def swap(self, params):
            """
            List FA2 tokens for sale.

            Adds this marketplace as an FA2 operator so it can transfer
            tokens on the seller's behalf at collect time.

            params:
              - token_id : sp.nat
              - amount   : sp.nat    (min 1)
              - price    : sp.mutez  (price per edition, must be > 0)
            """
            assert params.amount >= sp.nat(1), "MIN_ONE_EDITION"
            assert params.price  >  sp.mutez(0), "PRICE_MUST_BE_POSITIVE"

            self._add_operator(sp.record(
                fa2      = self.data.fa2_contract,
                owner    = sp.sender,
                token_id = params.token_id,
            ))

            self.data.swaps[self.data.next_swap_id] = sp.record(
                seller   = sp.sender,
                fa2      = self.data.fa2_contract,
                token_id = params.token_id,
                amount   = params.amount,
                price    = params.price,
            )

            self.data.next_swap_id += 1

        # ── collect (buy) ─────────────────────────────────────────────────

        @sp.entrypoint
        def collect(self, params):
            """
            Buy editions from a swap.

            Payment is distributed in order:
              1. Platform fee  → fee_recipient
              2. Royalties     → each recipient from FA2 get_royalties view
              3. Remainder     → seller

            Transfers tokens from seller to buyer via FA2 transfer.
            Auto-closes swap and revokes operator if fully sold.

            params:
              - swap_id  : sp.nat
              - quantity : sp.nat  (min 1)
            """
            assert self.data.swaps.contains(params.swap_id), "UNKNOWN_SWAP"

            swap = self.data.swaps[params.swap_id]

            assert params.quantity >= sp.nat(1),      "MIN_ONE_EDITION"
            assert params.quantity <= swap.amount,     "EXCEEDS_AVAILABLE"
            assert sp.sender != swap.seller,           "CANNOT_BUY_OWN_SWAP"

            total_price = sp.split_tokens(swap.price, params.quantity, sp.nat(1))
            assert sp.amount == total_price, "WRONG_AMOUNT"

            remaining = sp.local("remaining", sp.amount)

            # 1. Platform fee
            sp.if self.data.fee_percent > sp.nat(0):
                fee = sp.split_tokens(sp.amount, self.data.fee_percent, sp.nat(1000))
                sp.if fee > sp.mutez(0):
                    sp.send(self.data.fee_recipient, fee)
                    remaining.value = sp.utils.nat_to_mutez(
                        sp.utils.mutez_to_nat(remaining.value) -
                        sp.utils.mutez_to_nat(fee)
                    )

            # 2. Royalties from FA2 onchain view
            royalty_map = sp.View(swap.fa2, "get_royalties")(swap.token_id)
            sp.cast(royalty_map, sp.map[sp.address, sp.nat])

            for recipient, share in royalty_map.items():
                sp.if share > sp.nat(0):
                    royalty_pmt = sp.split_tokens(sp.amount, share, sp.nat(1000))
                    sp.if royalty_pmt > sp.mutez(0):
                        sp.if royalty_pmt <= remaining.value:
                            sp.send(recipient, royalty_pmt)
                            remaining.value = sp.utils.nat_to_mutez(
                                sp.utils.mutez_to_nat(remaining.value) -
                                sp.utils.mutez_to_nat(royalty_pmt)
                            )

            # 3. Remainder to seller
            sp.if remaining.value > sp.mutez(0):
                sp.send(swap.seller, remaining.value)

            # Transfer tokens seller → buyer via FA2
            fa2_xfer = sp.contract(
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
                fa2_xfer
            )

            # Update or close swap
            new_amount = sp.as_nat(swap.amount - params.quantity)

            sp.if new_amount == sp.nat(0):
                self._remove_operator(sp.record(
                    fa2      = swap.fa2,
                    owner    = swap.seller,
                    token_id = swap.token_id,
                ))
                del self.data.swaps[params.swap_id]
            sp.else:
                self.data.swaps[params.swap_id].amount = new_amount

        # ── cancel_swap ───────────────────────────────────────────────────

        @sp.entrypoint
        def cancel_swap(self, swap_id):
            """
            Cancel a swap. Only the original seller can cancel.
            Revokes marketplace operator rights on the FA2.
            """
            assert self.data.swaps.contains(swap_id), "UNKNOWN_SWAP"
            swap = self.data.swaps[swap_id]
            assert sp.sender == swap.seller, "NOT_SELLER"

            self._remove_operator(sp.record(
                fa2      = swap.fa2,
                owner    = swap.seller,
                token_id = swap.token_id,
            ))

            del self.data.swaps[swap_id]

        # ── admin: update FA2 contract address ────────────────────────────

        @sp.entrypoint
        def set_fa2_contract(self, new_fa2):
            """
            Admin can point the marketplace at a new FA2 contract.
            Does not affect existing swaps (they store the FA2 address at list time).
            """
            assert sp.sender == self.data.administrator, "NOT_ADMIN"
            self.data.fa2_contract = new_fa2

        # ── view: get_swap ────────────────────────────────────────────────

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

    admin = sp.test_account("Admin")
    alice = sp.test_account("Alice")   # seller
    bob   = sp.test_account("Bob")     # buyer
    carol = sp.test_account("Carol")   # royalty recipient

    # ── Deploy stub FA2 ───────────────────────────────────────────────────────

    scenario.h2("1. Deploy stub FA2 (10% royalty to Carol)")
    fa2 = marketplace_module.StubFA2(
        royalty_recipient = carol.address,
        royalty_share     = sp.nat(100),  # 100/1000 = 10%
    )
    scenario += fa2

    # Seed Alice with 10 editions of token 0
    fa2.set_balance(
        sp.record(owner=alice.address, token_id=sp.nat(0), amount=sp.nat(10)),
        _sender=admin
    )
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

    # ── Swap: Alice lists 5 editions at 2 tez ────────────────────────────────

    scenario.h2("3. Alice lists 5 editions of token 0 at 2 tez each")
    market.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(5), price=sp.tez(2)),
        _sender=alice
    )
    scenario.verify(market.data.next_swap_id == sp.nat(1))
    scenario.verify(market.data.swaps[sp.nat(0)].amount == sp.nat(5))
    scenario.verify(market.data.swaps[sp.nat(0)].price  == sp.tez(2))
    scenario.verify(fa2.data.operators.contains(sp.record(
        owner=alice.address, operator=market.address, token_id=sp.nat(0)
    )))

    # ── Collect: Bob buys 2 editions ──────────────────────────────────────────

    scenario.h2("4. Bob buys 2 editions at 2 tez each (4 tez total)")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(2)),
        _sender=bob,
        _amount=sp.tez(4)
    )
    scenario.verify(
        fa2.data.ledger[sp.pair(bob.address, sp.nat(0))] == sp.nat(2)
    )
    scenario.verify(market.data.swaps[sp.nat(0)].amount == sp.nat(3))

    # ── Wrong amount rejected ─────────────────────────────────────────────────

    scenario.h2("5. Bob underpays — should fail")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(1)),
        _sender=bob,
        _amount=sp.tez(1),  # should be 2 tez
        _valid=False
    )

    # ── Seller cannot buy own swap ────────────────────────────────────────────

    scenario.h2("6. Alice tries to buy her own swap — should fail")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(1)),
        _sender=alice,
        _amount=sp.tez(2),
        _valid=False
    )

    # ── Exceed available editions ─────────────────────────────────────────────

    scenario.h2("7. Bob tries to buy more than available — should fail")
    market.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(10)),
        _sender=bob,
        _amount=sp.tez(20),
        _valid=False
    )

    # ── Cancel swap ───────────────────────────────────────────────────────────

    scenario.h2("8. Alice cancels swap 0")
    market.cancel_swap(sp.nat(0), _sender=alice)
    scenario.verify(~market.data.swaps.contains(sp.nat(0)))
    scenario.verify(~fa2.data.operators.contains(sp.record(
        owner=alice.address, operator=market.address, token_id=sp.nat(0)
    )))

    # ── Non-seller cannot cancel ──────────────────────────────────────────────

    scenario.h2("9. Alice re-lists 3 editions; Bob tries to cancel — should fail")
    market.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(3), price=sp.tez(1)),
        _sender=alice
    )
    scenario.verify(market.data.next_swap_id == sp.nat(2))
    market.cancel_swap(sp.nat(1), _sender=bob, _valid=False)

    # ── Collect last edition closes swap automatically ────────────────────────

    scenario.h2("10. Bob buys all 3 — swap auto-closes, operator revoked")
    market.collect(
        sp.record(swap_id=sp.nat(1), quantity=sp.nat(3)),
        _sender=bob,
        _amount=sp.tez(3)
    )
    scenario.verify(~market.data.swaps.contains(sp.nat(1)))
    scenario.verify(~fa2.data.operators.contains(sp.record(
        owner=alice.address, operator=market.address, token_id=sp.nat(0)
    )))

    # ── Marketplace with 2.5% fee ─────────────────────────────────────────────

    scenario.h2("11. Deploy marketplace with 2.5% fee")
    market_fee = marketplace_module.REJKTMarketplace(
        admin_address = admin.address,
        fa2_contract  = fa2.address,
        fee_percent   = sp.nat(25),   # 25/1000 = 2.5%
        fee_recipient = admin.address,
    )
    scenario += market_fee
    scenario.verify(market_fee.data.fee_percent == sp.nat(25))

    # Alice lists 2 editions at 10 tez on fee marketplace
    # (Alice still has 10 - 2 - 3 = 5 editions left)
    market_fee.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(2), price=sp.tez(10)),
        _sender=alice
    )
    # Bob collects 1:
    #   fee     = 10 tez * 25/1000  = 0.25 tez → admin
    #   royalty = 10 tez * 100/1000 = 1.0  tez → carol
    #   seller  = 10 - 0.25 - 1.0  = 8.75 tez → alice
    market_fee.collect(
        sp.record(swap_id=sp.nat(0), quantity=sp.nat(1)),
        _sender=bob,
        _amount=sp.tez(10)
    )
    # Bob now has 2 (test 4) + 3 (test 10) + 1 (test 11) = 6 editions
    scenario.verify(
        fa2.data.ledger[sp.pair(bob.address, sp.nat(0))] == sp.nat(6)
    )

    # ── Zero price rejected ───────────────────────────────────────────────────

    scenario.h2("12. Alice tries to list at 0 price — should fail")
    market.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(1), price=sp.mutez(0)),
        _sender=alice,
        _valid=False
    )

    # ── Zero quantity rejected ────────────────────────────────────────────────

    scenario.h2("13. Alice tries to list 0 editions — should fail")
    market.swap(
        sp.record(token_id=sp.nat(0), amount=sp.nat(0), price=sp.tez(1)),
        _sender=alice,
        _valid=False
    )

    # ── Admin: update FA2 contract address ────────────────────────────────────

    scenario.h2("14. Admin updates FA2 contract address")
    market.set_fa2_contract(sp.address("KT1Fake"), _sender=admin)
    scenario.verify(market.data.fa2_contract == sp.address("KT1Fake"))

    # Non-admin cannot update
    market.set_fa2_contract(fa2.address, _sender=alice, _valid=False)

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
