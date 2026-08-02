# Telegram Sales Bot (Ollama)

A Telegram chatbot that talks to customers, answers questions from your product
catalog, handles objections, and pushes them toward a purchase — powered by a
local LLM running on your droplet via **Ollama**. When a customer shows they're
ready to buy, it captures the lead and pings you on Telegram so you can close.

```
Customer ──▶ Telegram ──▶ bot (this repo) ──▶ Ollama (your droplet)
                              │
                              └─▶ notifies you + saves the lead
```

## How it all connects

Everything below is already wired — there's no glue code left to write, only
three credentials to fill in. A single message flows like this:

```
Customer messages your @handle
        │
        ▼
Telegram  ──(long-polling)──▶  bot (run.py)
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                         ▼
  mid-checkout?            normal message              /buy command
  verify payment ID     system prompt (your          product buttons →
  (deterministic)     catalog) + chat history →      pick provider →
        │                 Ollama model on your        pay → send ID →
        │                    droplet → reply           verify → deliver
        ▼                        │                         │
   deliver / notify         reply to customer        notify you on a sale
```

- **"It talks based on its training"** → the message goes to your Ollama model
  with a *system prompt built from `catalog.json`*, so it answers in its own
  words but only sells what you actually offer.
- **"It sells based on the request"** → the same prompt tells it to qualify the
  buyer, recommend the best-fit product, and send them to `/buy` to pay.
- **The three plugs you provide:** a Telegram bot token, a running Ollama with a
  model pulled, and your `catalog.json`. `python run.py --check` confirms all
  three are connected before you go live.

## Fully autonomous mode (Stripe + inventory) ⭐

Set `STRIPE_SECRET_KEY` and the bot runs **end-to-end with zero human touch**:

```
Customer: /buy
  → picks a product (sold-out items are marked)
  → bot RESERVES one item from inventory  (so you never oversell)
  → bot creates a Stripe checkout link (card / Apple Pay / Cash App Pay)
  → customer pays
  → bot POLLS Stripe in the background until paid   (no webhook/domain needed)
  → bot pulls that reserved item from the database and DELIVERS it instantly
  → you get a "💰 Sale" notification with stock remaining
```

No paste-your-ID step, no Approve/Reject, no manual fulfillment. Payment is
verified by Stripe's API; delivery comes straight from your inventory DB.

**Why it's safe to run unattended:**
- **No oversell** — an item is reserved at checkout; if payment expires, it's
  released back to stock automatically.
- **No double-charge delivery** — each Stripe payment is claimed once in a
  ledger; repeated poll ticks are no-ops.
- **Restart-proof** — the poller reconciles from the database on startup, so a
  reboot mid-checkout still delivers once payment clears.
- **Never delivers nothing** — because stock is reserved up front, a paid order
  always has an item; the one impossible edge case is caught and flagged to you.

**Loading inventory** (one deliverable per line — keys, accounts, links):
```bash
python run.py stock <product_id> keys.txt   # add items
python run.py stock                          # show counts
# or in Telegram (seller only):  /stock
```

**Requirements for true autonomy:** a Stripe account, and products that are
**digitally deliverable** from that stock list. Venmo / personal Cash App are
*not* used here — they have no verification API and can't be autonomous (see the
manual-provider flow below if you still want them).

## What it does

- 💬 **Natural sales conversations** — the LLM plays a friendly, on-brand seller.
- 📦 **Knows your products** — reads them from `catalog.json` (name, price,
  summary, details, FAQ). No code changes to update your offers.
- 🧠 **Remembers the chat** — per-customer history stored in SQLite.
- 🔥 **Captures hot leads** — detects buying intent, saves it, and DMs you.
- 💳 **Guides to checkout** — sends your payment link, or hands the lead to you
  if you'd rather close manually.
- 🛡️ **Stays honest** — instructed never to invent prices, discounts, or
  features, and never to ask for card numbers/passwords in chat.

## Commands

| Command  | Who       | What                                    |
|----------|-----------|-----------------------------------------|
| `/start`  | anyone    | Greeting + restart the conversation    |
| `/help`   | anyone    | How to use the bot                     |
| `/reset`  | anyone    | Clear conversation memory              |
| `/buy`    | anyone    | Pick a product and pay (if enabled)    |
| `/leads`  | you only  | List recent captured leads             |
| `/orders` | you only  | List recent orders + their status      |
| `/stock`  | you only  | Show inventory counts per product      |

---

## 1. Prerequisites on the droplet

You said you already have a droplet with Ollama. Make sure a model is pulled:

```bash
# on the droplet
ollama pull llama3.1:8b        # good default; ~5GB, needs ~8GB RAM
# smaller/faster if the droplet is small:
# ollama pull llama3.2:3b
ollama list                    # confirm it's there
```

The bot talks to Ollama over HTTP at `OLLAMA_HOST`.
**Run the bot on the same droplet as Ollama** and keep `OLLAMA_HOST=http://127.0.0.1:11434`
— that's simplest and keeps Ollama off the public internet.

> If you must run the bot elsewhere, expose Ollama carefully:
> set `OLLAMA_HOST=0.0.0.0` in Ollama's environment and restrict access with a
> firewall / private network / VPN. Never leave port `11434` open to the world.

## 2. Create the Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts.
2. Copy the **bot token** it gives you.
3. Message **@userinfobot** to get **your** numeric user ID (for lead alerts).

## 3. Install

```bash
# on the droplet
git clone -b claude/telegram-sales-ollama-chatbot-v007fe <this-repo> salesbot
cd salesbot
./setup.sh            # creates venv, installs deps, scaffolds .env, runs preflight
```

`setup.sh` is safe to re-run. It'll tell you to edit `.env`, then re-run it to
verify every connection. (Manual equivalent: `python3 -m venv .venv &&
source .venv/bin/activate && pip install -r requirements.txt`.)

## 4. Configure

```bash
cp .env.example .env
nano .env
```

Fill in at least:

- `TELEGRAM_BOT_TOKEN` — from BotFather
- `SELLER_CHAT_IDS` — your numeric ID (comma-separate for multiple people)
- `OLLAMA_MODEL` — the model you pulled (e.g. `llama3.1:8b`)
- `BUSINESS_NAME` — your client's business name
- `CHECKOUT_URL` — *(optional)* a Stripe/crypto/etc. checkout link. Leave blank
  to have the bot collect contact info and hand the lead to you instead.

Then edit **`catalog.json`** with your client's real products, prices, and FAQ.
This file is the single source of truth the bot sells from.

## 5. Run it

**First, verify every connection is live** (token, Ollama, model, catalog):

```bash
python run.py --check
```

You'll get a pass/fail checklist with the exact fix for anything broken —
including Stripe connectivity and per-product stock levels.

**If you're using autonomous mode, load your inventory** (one item per line):

```bash
python run.py stock pro pro_keys.txt
```

Once `--check` says *Ready to sell*, start the bot:

```bash
python run.py
```

Open Telegram, message your bot, and try: *"what do you sell?"*, then
*"I'll take the pro one"* — you should get a lead notification.

## 6. Keep it running 24/7 (systemd)

```bash
./setup.sh --systemd     # generates the unit with the right paths/user, starts it
journalctl -u salesbot -f        # live logs
sudo systemctl restart salesbot  # after editing .env or catalog.json
```

(Prefer to do it by hand? A template lives at `deploy/salesbot.service` — edit
the paths/user, `sudo cp` it into `/etc/systemd/system/`, then
`daemon-reload && enable --now`.)

---

## Accepting & verifying payments

The bot can take payment and **verify the payment ID before delivering**. Turn
it on by listing methods in `PAYMENT_PROVIDERS` (e.g. `paypal,cashapp,applepay,venmo`).

### The buy flow

```
Customer: /buy
  → picks a product      (buttons)
  → picks how to pay     (buttons: only the methods you enabled)
  → gets pay instructions + your handle/link
  → pays, then sends the payment/transaction ID
  → bot verifies it ──┬─ verified ────────────▶ delivers + pings you
                      └─ can't auto-verify ───▶ you get Approve/Reject buttons
```

Money handling is **deterministic and button-driven** — the LLM sells, but it
**never** decides that a payment is valid. Verification happens only through the
provider's API or your explicit approval.

### What can actually be verified (read this)

Not all of these have a public API. Here's the honest picture:

| Method | Auto-verify? | How |
|---|---|---|
| **PayPal** | ✅ Yes | Official REST API — checks status, amount, currency, payer. |
| **Cash App** | ⚠️ Via **Square** only | Square "Cash App Pay" → Square Payments API. A personal `$cashtag` P2P payment has **no public API**. |
| **Apple Pay** | ⚠️ Via a **processor** only | Apple Pay is a card wallet, not a P2P app. Verified through Square (Payments API). Apple *Cash* (iMessage P2P) has **no API**. |
| **Venmo** | ❌ No public API | Personal Venmo can't be verified programmatically — routed to **you** to approve. |

When a method has no API (personal Cash App, Venmo, Apple Cash, or when you
haven't added API keys), the bot **still captures the payment ID** and asks
**you** to Approve/Reject in Telegram. Nothing is delivered until you confirm.

> ⚠️ Anyone offering to "verify Venmo/Cash App payment IDs" without a merchant
> account is either scraping (breaks ToS, breaks often) or just trusting what the
> buyer types. This bot doesn't pretend — it verifies what's verifiable and puts
> a human in the loop for the rest.

### Built-in safeguards

- **Double-spend ledger** — every accepted payment ID is recorded; the same ID
  can't be reused on another order (checked before the API call *and* atomically
  on approval).
- **Amount + currency match** — a payment for the wrong amount/currency is never
  auto-accepted; it goes to you for review.
- **Human-in-the-loop** — mismatches, reused IDs, and no-API methods all require
  your tap before anything ships.

### Setup per method

**PayPal (real verification):**
1. Create REST API credentials at
   <https://developer.paypal.com/dashboard/applications>.
2. On that app, enable **Transaction Search** (so PayPal.me / received payments
   can be looked up).
3. Set `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET`, `PAYPAL_ENV=live`, and optionally
   `PAYPAL_ME` (your paypal.me handle, used to build a pay link).

**Cash App / Apple Pay (via Square):**
1. Create a Square app at <https://developer.squareup.com/apps> and copy the
   **Access Token**.
2. Set `SQUARE_ACCESS_TOKEN` and `SQUARE_ENV=production`.
3. For Cash App, set your `$cashtag` in `CASHAPP_CASHTAG`.
4. For Apple Pay, give customers a Square payment link in `APPLEPAY_LINK`
   (or reuse `CHECKOUT_URL`).

**Venmo (human-verified):**
- Just set `VENMO_HANDLE`. You'll approve each payment in Telegram.

### Auto-delivery

Give a product a `"deliverable"` in `catalog.json` (a license key, a link, a
download, instructions). On a confirmed payment the bot DMs it to the buyer
automatically. Leave it empty and the bot just tells the buyer you'll set them
up, and pings you to fulfill.

### Before you take real money

- **Fees & accounts:** PayPal/Square charge processing fees and may require a
  business account. P2P methods (personal Venmo/Cash App) aren't meant for
  business sales and can freeze accounts — use the merchant options for volume.
- **Keep a human check for large orders.** The Approve/Reject flow is there for
  exactly this.
- **Comply with the providers' terms** and your local tax/consumer rules.

---

## Sounding like a real person

By default the bot chats like a human texting, not a bot writing essays. Three
things do the work:

- **A persona** — set `PERSONA_NAME` (e.g. `Alex`) and optional `PERSONA_STYLE`
  (e.g. *"laid-back, a bit of slang, sneaker nerd"*). The model stays in
  character and won't refer to itself as an AI or bot.
- **A low-key, non-salesy posture** (the default voice) — it doesn't pitch or
  chase. It's conversational and a little skeptical: it feels the buyer out,
  makes *them* explain why it's a fit, holds back detail, and uses takeaways
  ("no rush", "not really for everyone") so the buyer leans in instead of being
  pushed. It only mentions `/buy` once the buyer clearly wants it. Tune the
  intensity with `PERSONA_STYLE`.
- **Short chat bubbles** — replies are split into a few short messages instead
  of one long block (`MAX_BUBBLES`).
- **Realistic typing** — a "typing…" indicator with a delay proportional to
  message length (`TYPING_CPS`), plus markdown/bullets stripped out — real
  people don't send **bold** text or bullet lists.

Turn it all off with `HUMANIZE=false` for plain, instant single-message replies.

> ⚠️ **One honest caveat.** A warm, named persona is normal and fine. But making
> the bot *actively deny being a bot* when a customer directly asks is a
> different thing — some places (e.g. California's bot-disclosure law) require
> disclosure for sales, and buyers who feel tricked file more chargebacks. The
> default prompt keeps the persona natural without constructing fake proof of
> being human; decide your own policy for the "are you a real person?" question.

## Tuning for more sales

- **Catalog copy matters most.** Punchy `summary` lines and clear `details`
  give the LLM better material to sell with.
- **Model choice.** `llama3.1:8b` is a solid balance. A bigger model sells more
  persuasively but is slower; a 3B model is snappier on a small droplet.
- **Persona.** Edit `app/sales.py` → `build_system_prompt()` to change tone,
  add discounts/scarcity, or tighten objection handling.
- **Buying signals.** `detect_buying_signal()` in `app/sales.py` controls when a
  lead fires — add phrases your customers actually use.
- **Temperature.** Lower `OLLAMA_TEMPERATURE` (e.g. `0.4`) for more consistent,
  on-script replies; higher for more personality.

## Where things live

```
app/
  bot.py            # Telegram handlers + main loop
  config.py         # loads .env + catalog.json
  ollama_client.py  # async Ollama chat client
  sales.py          # persona/system prompt + buying-signal detection
  humanize.py       # chat bubbles + typing delays + markdown stripping
  store.py          # SQLite: conversations, leads, orders, payment ledger
  checkout_flow.py  # ⭐ autonomous Stripe checkout + inventory delivery + poller
  payments_flow.py  # manual paste-ID flow (used when Stripe isn't configured)
  stock_cli.py      # load inventory:  python run.py stock <id> <file>
  doctor.py         # preflight connection check (python run.py --check)
  payments/
    stripe_gateway.py  # Stripe hosted checkout + status polling
    base.py         # verifier interface + result type
    paypal.py       # PayPal REST verification
    square.py       # Cash App Pay + Apple Pay (Square Payments API)
    manual.py       # human-approved fallback (Venmo, P2P Cash App)
    registry.py     # builds active verifiers from config
catalog.json        # YOUR products / prices / FAQ  ← edit this
.env.example        # config template  → copy to .env
run.py              # entry point:  python run.py
tests/test_payments.py    # payment verification tests (mocked HTTP)
tests/test_checkout.py    # Stripe checkout + inventory + delivery tests
deploy/salesbot.service   # systemd unit for 24/7 running
```

## Data & privacy

Conversations and leads are stored in a local SQLite file (`data/leads.db`,
git-ignored). Nothing leaves your droplet except Telegram messages and the
Ollama calls (which are local). Tell customers a human may follow up, and
comply with your local rules on messaging and data.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot starts but replies "having a hiccup" | Ollama unreachable or model missing. `ollama list`, then `ollama pull <model>`; check `OLLAMA_HOST`. |
| "TELEGRAM_BOT_TOKEN is missing" on start | You didn't copy `.env.example` to `.env` or didn't set the token. |
| No lead notifications | `SELLER_CHAT_IDS` is empty/wrong. Get your ID from @userinfobot. You must have messaged the bot at least once. |
| Slow replies | Use a smaller model, lower `OLLAMA_NUM_CTX`, or a bigger droplet. |
