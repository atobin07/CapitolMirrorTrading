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
git clone <this-repo> /opt/salesbot
cd /opt/salesbot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

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

You'll get a pass/fail checklist with the exact fix for anything broken. Once
it says *Ready to sell*, start the bot:

```bash
python run.py
```

Open Telegram, message your bot, and try: *"what do you sell?"*, then
*"I'll take the pro one"* — you should get a lead notification.

## 6. Keep it running 24/7 (systemd)

```bash
# edit paths/user inside the file first
sudo cp deploy/salesbot.service /etc/systemd/system/salesbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now salesbot
journalctl -u salesbot -f        # live logs
```

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
  sales.py          # system prompt + buying-signal detection
  store.py          # SQLite: conversations, leads, orders, payment ledger
  payments_flow.py  # /buy → pay → verify → deliver (button-driven)
  doctor.py         # preflight connection check (python run.py --check)
  payments/
    base.py         # verifier interface + result type
    paypal.py       # PayPal REST verification
    square.py       # Cash App Pay + Apple Pay (Square Payments API)
    manual.py       # human-approved fallback (Venmo, P2P Cash App)
    registry.py     # builds active verifiers from config
catalog.json        # YOUR products / prices / FAQ / deliverables  ← edit this
.env.example        # config template  → copy to .env
run.py              # entry point:  python run.py
tests/test_payments.py    # payment verification tests (mocked HTTP)
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
