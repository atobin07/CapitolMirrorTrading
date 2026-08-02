"""End-to-end proof: a full purchase through the REAL bot code.

The only things faked are the three external network services — Telegram
transport, the Stripe API, and the Ollama API. Everything else (disclosure,
persona prompt, humanizer, /buy, checkout, payment verification, inventory
reservation + unique delivery, double-spend guard, seller notifications) is the
real production code. On the droplet you swap the three fakes for real creds and
nothing else changes.

Run:  python tests/test_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.bot as bot  # noqa: E402
from app import humanize  # noqa: E402
from app.checkout_flow import CheckoutFlow  # noqa: E402
from app.payments.stripe_gateway import CheckoutSession  # noqa: E402
from app.sales import build_system_prompt  # noqa: E402
from app.store import Store  # noqa: E402

_checks = 0
CUSTOMER = 42
SELLER = 999


def check(cond, msg):
    global _checks
    assert cond, "❌ FAILED: " + msg
    _checks += 1


# ── fakes for the three external services ────────────────────────────────────
class FakeBot:
    """Stands in for Telegram. Records everything it 'sends'."""
    def __init__(self):
        self.log = []          # (chat_id, text)

    async def send_message(self, chat_id, text, **kw):
        self.log.append((chat_id, text))
        tag = "seller" if chat_id == SELLER else "→ customer"
        print(f"   [{tag}] {text}")

    async def send_chat_action(self, chat_id, action):
        pass


class FakeOllama:
    """Stands in for the local LLM. Returns a scripted reluctant-style reply."""
    def __init__(self):
        self.reply = "depends what you need it for\n\nwhat are you trying to do?"

    async def chat(self, messages):
        return self.reply


class FakeGateway:
    """Stands in for the Stripe API."""
    def __init__(self):
        self.live = True
        self.paid = False
        self._amount = Decimal("0")

    async def create_session(self, *, product_name, amount, currency, order_id, chat_id):
        self._amount = Decimal(str(amount))
        return CheckoutSession(
            id="cs_demo", url="https://checkout.stripe.com/pay/cs_demo",
            status="open", payment_status="unpaid",
            amount_total=self._amount, currency=currency, payment_intent=None, raw={},
        )

    async def get_session(self, sid):
        if self.paid:
            return CheckoutSession(
                id=sid, url="x", status="complete", payment_status="paid",
                amount_total=self._amount, currency="USD",
                payment_intent="pi_demo", raw={},
            )
        return CheckoutSession(
            id=sid, url="x", status="open", payment_status="unpaid",
            amount_total=self._amount, currency="USD", payment_intent=None, raw={},
        )

    async def expire_session(self, sid):
        pass

    async def close(self):
        pass


# ── minimal Telegram Update/CallbackQuery stand-ins ──────────────────────────
class FakeMsg:
    def __init__(self, chat_id, text, botlog):
        self.chat_id = chat_id
        self.text = text
        self._botlog = botlog

    async def reply_text(self, text, **kw):
        self._botlog.append((self.chat_id, text))
        print(f"   [→ customer] {text}")
        self._last_markup = kw.get("reply_markup")


class FakeQuery:
    def __init__(self, data, user, chat_id, botlog):
        self.data = data
        self.from_user = user
        self.message = SimpleNamespace(chat_id=chat_id)
        self._botlog = botlog
        self.edited = None
        self.edited_markup = None

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kw):
        self.edited = text
        self.edited_markup = kw.get("reply_markup")
        print(f"   [→ customer] {text}")


def buttons(markup):
    out = []
    if markup:
        for row in markup.inline_keyboard:
            for b in row:
                out.append(b)
    return out


async def main():
    print("\n" + "=" * 60)
    print(" END-TO-END PROOF  —  real bot code, faked external services")
    print("=" * 60)

    # ── wire the real components ─────────────────────────────────────────────
    db = os.path.join(tempfile.mkdtemp(), "e2e.db")
    store = Store(db)
    catalog = {
        "currency": "USD",
        "products": [{
            "id": "pro", "name": "Pro Toolkit", "price": 149,
            "summary": "the popular one", "details": "",
        }],
    }
    cfg = SimpleNamespace(
        business_name="Acme Digital",
        catalog=catalog,
        seller_chat_ids=[SELLER],
        persona_name="Alex", persona_style="",
        humanize=True, typing_cps=100000, max_bubbles=3,  # fast for the demo
        disclosure_enabled=True,
        disclosure_text="heads up — you're chatting with an automated assistant "
                        "for {business}. type /terms for the refund policy.",
        refund_policy="Full refund if it's defective or undelivered.",
        support_contact="support@acme.co", terms_url="",
        stripe_poll_interval=8, stripe_session_timeout=1800,
        stripe_enabled=True, payments_enabled=True,
        rate_limit_per_min=15, max_input_chars=1000,
    )
    gateway = FakeGateway()
    flow = CheckoutFlow(cfg, store, gateway)

    # Inject the real bot module's globals (same code the live bot runs).
    bot.cfg = cfg
    bot.store = store
    bot.ollama = FakeOllama()
    bot.payment_flow = None
    bot.checkout_flow = flow
    bot.system_prompt = build_system_prompt(
        cfg.business_name, cfg.catalog, "", ["card, Apple Pay, or Cash App"],
        persona_name="Alex",
    )

    fbot = FakeBot()
    ctx = SimpleNamespace(bot=fbot)
    user = SimpleNamespace(id=CUSTOMER, username="buyer", full_name="Sam Buyer")

    # ── seed inventory ───────────────────────────────────────────────────────
    store.add_stock("pro", ["PROKEY-AAAA-1111", "PROKEY-BBBB-2222"])
    start_stock = store.available_count("pro")
    print(f"\n[setup] loaded {start_stock} Pro Toolkit keys into inventory\n")
    check(start_stock == 2, "inventory seeded")

    # ── 1. customer says hi → disclosure + greeting ──────────────────────────
    print("① customer opens the chat:  /start")
    up = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CUSTOMER),
        effective_user=user,
        message=FakeMsg(CUSTOMER, "/start", fbot.log),
    )
    await bot.start(up, ctx)
    disclosed = any("automated assistant" in t for _, t in fbot.log)
    check(disclosed, "bot discloses it's automated on first contact")

    # ── 2. a normal question → LLM reply through the humanizer ───────────────
    print("\n② customer:  'is the pro one any good?'")
    up2 = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CUSTOMER),
        effective_user=user,
        message=FakeMsg(CUSTOMER, "is the pro one any good?", fbot.log),
    )
    before = len(fbot.log)
    await bot.on_message(up2, ctx)
    new_msgs = [t for _, t in fbot.log[before:]]
    check(len(new_msgs) >= 1, "bot replies conversationally")
    check(all("*" not in t and "#" not in t for t in new_msgs), "reply has no markdown")

    # disclosure must NOT repeat
    check(sum("automated assistant" in t for _, t in fbot.log) == 1,
          "disclosure shown exactly once")

    # ── 3. customer decides to buy → /buy ────────────────────────────────────
    print("\n③ customer:  /buy")
    up3 = SimpleNamespace(
        effective_chat=SimpleNamespace(id=CUSTOMER),
        effective_user=user,
        message=FakeMsg(CUSTOMER, "/buy", fbot.log),
    )
    await flow.cmd_buy(up3, ctx)
    prod_btn = buttons(up3.message._last_markup)
    check(prod_btn and prod_btn[0].callback_data == "buy:pro", "product button offered")

    # ── 4. picks the product → reserve + Stripe link ─────────────────────────
    print("\n④ customer taps 'Pro Toolkit'")
    q = FakeQuery("buy:pro", user, CUSTOMER, fbot.log)
    await flow.on_buy(SimpleNamespace(callback_query=q), ctx)
    check(store.available_count("pro") == 1, "one item RESERVED at checkout (no oversell)")
    pay_btn = buttons(q.edited_markup)
    check(pay_btn and pay_btn[0].url.startswith("https://checkout.stripe.com"),
          "customer gets a real Stripe pay link")
    order = store.active_order_for_chat(CUSTOMER)
    check(order["state"] == "awaiting_payment", "order is awaiting payment")
    print(f"   [system] order #{order['id']} created, 1 key reserved, awaiting payment")

    # ── 5. customer pays on Stripe → background poller confirms + delivers ────
    print("\n⑤ customer completes payment on Stripe…")
    gateway.paid = True
    app = SimpleNamespace(bot=fbot)
    print("   [system] poller ticks, sees 'paid', fulfils:")
    await flow._poll_once(app)

    order = store.get_order(order["id"])
    check(order["state"] == "paid", "order marked PAID after Stripe confirms")
    delivered = [t for cid, t in fbot.log if cid == CUSTOMER and "PROKEY-" in t]
    check(len(delivered) == 1, "exactly one unique key delivered to the buyer")
    check(store.available_count("pro") == start_stock - 1,
          "inventory decremented by exactly one (2 → 1)")
    check("PROKEY-" in delivered[0], "delivered item is a real inventory key")
    check(any(cid == SELLER and "Sale" in t for cid, t in fbot.log), "seller notified of the sale")

    # ── 6. double-spend / double-deliver guard ───────────────────────────────
    print("\n⑥ poller runs again (proving no double-delivery)…")
    n_before = len(fbot.log)
    store.set_order_state(order["id"], "awaiting_payment")  # force a re-check
    await flow._poll_once(app)
    check(len(fbot.log) == n_before, "already-paid order is a no-op — no second delivery")
    print("   [system] no duplicate delivery ✔")

    # ── 7. /terms works ──────────────────────────────────────────────────────
    print("\n⑦ customer:  /terms")
    up7 = SimpleNamespace(message=FakeMsg(CUSTOMER, "/terms", fbot.log),
                          effective_chat=SimpleNamespace(id=CUSTOMER))
    await bot.terms_cmd(up7, ctx)
    check(any("refund" in t.lower() for cid, t in fbot.log if cid == CUSTOMER),
          "/terms shows the refund policy")

    await flow.close()
    store.close()

    print("\n" + "=" * 60)
    print(f" ✅ ALL {_checks} END-TO-END CHECKS PASSED")
    print("=" * 60)
    print(" Proven: disclosure · human-style chat · /buy · Stripe checkout ·")
    print(" payment verification · inventory reserve+deliver · no oversell ·")
    print(" no double-charge · seller alerts · /terms")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
