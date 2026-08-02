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
| `/start` | anyone    | Greeting + restart the conversation     |
| `/help`  | anyone    | How to use the bot                      |
| `/reset` | anyone    | Clear conversation memory               |
| `/leads` | you only  | List recent captured leads              |

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
  store.py          # SQLite: conversations + leads
catalog.json        # YOUR products / prices / FAQ  ← edit this
.env.example        # config template  → copy to .env
run.py              # entry point:  python run.py
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
