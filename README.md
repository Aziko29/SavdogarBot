# Smart SMM-Sales Auto-Bot (SavdogarBot)

Telegram bot that watches a SOURCE channel, rewrites each photo post into persuasive Uzbek sales copy with AI
(Gemini first, OpenAI-compatible fallbacks) and re-posts products to a TARGET channel on a schedule.
Customers order through a deep link (`/start prod_<id>`); admins accept or reject orders in the bot.

## 1. Setup
1. Create a bot with **@BotFather** and copy the token.
2. Add the bot as **admin of the SOURCE channel** (this is how it receives channel posts).
3. Add the bot as **admin of the TARGET channel** with *Post*, *Edit* and *Delete messages* rights.
4. Get the channel IDs (they look like `-1001234567890`, e.g. forward a post to @RawDataBot) and the main admin's user ID (@userinfobot).
5. Get AI keys: Gemini from Google AI Studio (primary); optionally Groq, OpenRouter, OpenAI or Qwen (fallbacks).
6. The main admin (and every admin added later) must open a private chat with the bot and press **Start**, otherwise it cannot message them.

## 2. Install and run
```bash
python3.12 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # then edit .env
python main.py
```
Send `/admin` to the bot in a private chat for settings, product management and status.

## 3. `.env` guide
- Required: `BOT_TOKEN`, `BOSH_ADMIN_ID`.
- **Channels and groups are not in `.env`.** The main admin adds the bot to the source channel (as admin) and to the posting channel/group, then picks the role in the bot: the bot writes to him when it is added, or he opens `/admin` → **Kanal va guruhlar** and adds a chat by forwarding a post, sending its ID or @username. Only the main admin sees that section. A chat the bot was added to but that is not registered within `FOREIGN_CHAT_GRACE_HOURS` (default 6) hours is left by the bot automatically (it also starts counting when you remove a chat from the list); the section lists such waiting chats with buttons to register them. Several posting places are supported: every product is published to all of them.
- Optional: `OWNER_ID` (server owner: receives the log files, no panel rights by itself; he must press Start in the bot once), `LOG_SEND_HOURS` (how often new log lines are sent to the owner, default 24, `0` = off), `LOCK_PORT` (unique per bot when several bots share one server: 47400, 47401, ...).
- Admins: `BOSH_ADMIN_ID` is the main admin (changeable only in `.env`). He opens `/admin` → **Adminlar** to add or remove other admins by Telegram ID or a forwarded message; they are stored in the database. Only the main admin sees that section, and he can never be removed from inside the bot. The same goes for **Sozlamalar** (autopost interval, night window, weights, repost policy): regular admins don't get the button, and a stale or hand-made tap is refused.
- `ADMIN_IDS` is no longer used; the bot refuses to start while it is set without `BOSH_ADMIN_ID`.
- Gemini: `GEMINI_API_KEYS` (group 1), `GEMINI_API_KEYS_2`, … (later groups), `GEMINI_MODELS` in priority order.
- Fallbacks: `FALLBACK_PROVIDERS=groq,openrouter`, then per provider `GROQ_API_KEYS`, `GROQ_MODELS`
  and optionally `GROQ_BASE_URL` (mandatory for custom provider names).
- Model names are never hard-coded; check the providers' current model lists before filling them in.
- Tuning (AI workers, timeouts, repost gap, photo size, album wait) is documented in `.env.example`.
- Autopost interval, night window, category weights and repost policy are changed in `/admin`, not in `.env`.
- Quiet inquiries: `INQUIRY_IDLE_HOURS` (default 1, range 0.05-24): how long a claimed inquiry may stay silent before the admins are asked to continue or end it; see section 4.
- Order reminders: `ORDER_REMIND_AFTER_MIN` (default 15), `ORDER_REMIND_EVERY_MIN` (default 15) and `ORDER_REMIND_MAX` (default 3, `0` = off); see section 4.

## 4. How it works
- Post a photo with a caption in the SOURCE channel; edits re-run the AI. The same photo is never added twice.
- Products are ranked new / mid / old (weights and keep-counts in `/admin`) and picked by weighted random choice.
- Telegram does not report channel-post deletions to bots: mark a product `removed` or `sold` in the admin panel.
- **Post ID:** every post in the TARGET channel shows `🆔 ID: <n>`, where `<n>` is the product's database id (`products.id`, the `#<n>` in the admin panel and the `prod_<n>` in the buy button's link). Captions stored before this feature get the line automatically at startup and again right before they are posted or edited; each published message is recorded in `post_log` with its Telegram message id.
- **AI re-polish:** when an admin edits a product field (name, price, size, fabric, stock) and saves, the value is stored at once, then the AI rewrites the sales pitch and hashtags around the corrected data and the live channel posts are edited to match the database. The AI can never change the admin's values, and a pitch that contains a number not present in the product data is rejected (the plain caption with the admin's data is used instead). If the admin makes another edit while the AI is working, the outdated result is discarded and the newer edit is polished.

## 4a. Orders
The button under a channel post opens the product in the bot with two choices: **Buyurtma qilish** (order) and **Admin bilan bog'lanish** (contact the admin, see below).

**Buyurtma qilish** opens an order form, one step at a time (every step can be abandoned with `/cancel`, the form expires after 30 minutes of inactivity):
1. **Quantity** (buttons 1-5 or a typed number), 2. **phone** (the "share my number" button or typed; a 9-digit local number gets `+998`), 3. **address** (typed, a shared location, or "Olib ketaman" for pick-up), 4. optional **note** (text, voice or photo), 5. a summary with **Tasdiqlash / Bekor qilish**.

Only after the customer confirms is the order created and sent to every admin as a card with the quantity, phone, address and note. The lifecycle of an order:
- `pending` -> the admin taps **Qabul qildim** (`accepted`, the live chat with the customer opens) or **Qolmagan** (`rejected`).
- `accepted` -> **Bajarildi** (completed) or **Bekor qilish** (cancelled). Either one closes the chat and tells the customer. The card also has **Suhbatni yakunlash** (end the chat only) and **Tugadi** (mark the product sold).
- While an order is `pending` the customer can withdraw it: `/buyurtmalarim` lists his latest orders with their status and a cancel button; the admins' cards are updated and they get a short notice.
- `/admin` -> **Buyurtmalar** is a menu with the counts: **Kutilayotgan** (pending cards), **Jarayonda** (accepted, not yet finished cards), **Yakunlangan** (compact text list of the latest finished orders) and **Tozalash**.
- **Tozalash** deletes finished orders (completed, cancelled, rejected) - all of them or only those older than 7 days - after a confirmation, together with their cards and relayed messages in the admins' chats. Pending and in-progress orders are never touched.
- **Admin bilan bog'lanish:** the customer writes his question (text, voice, photo). Every admin gets a card with the customer, the product (name, price and id) and a **Qabul qilish** button. The first admin to tap it (or to reply to the card) owns the chat: the other cards become "taken", and from then on the customer's messages reach only that admin. The customer ends the chat with `/cancel`, an admin with **Suhbatni yakunlash**; an inquiry with no customer activity for 24 hours stops relaying. If the customer already has an open order chat, he simply writes there.
- **Quiet inquiries:** if nobody writes in a claimed inquiry for `INQUIRY_IDLE_HOURS` hours (default 1), the chat is *not* closed. Every admin gets a card with **Davom ettirish** and **Suhbatni yakunlash**. The first admin to tap **Davom ettirish** takes the chat over and is sent the customer, the product with its id and the conversation so far (the last 50 messages); from then on the customer's messages reach him alone. If someone writes again before anyone decides, the cards are withdrawn. The check runs every 2 minutes (`inquiry_idle.py`). Order chats are unchanged: their messages still go to every admin.
- **Reminders:** a pending order that nobody decided within `ORDER_REMIND_AFTER_MIN` minutes is repeated to every admin (as a reply to the order card), then again every `ORDER_REMIND_EVERY_MIN` minutes, at most `ORDER_REMIND_MAX` times. The check runs every 2 minutes and does not depend on the night window.

Existing databases are upgraded automatically at startup (schema v6 adds the new order columns, v7 the inquiry tables, v8-v9 the inquiry claim columns and the card/transcript tables); old orders show quantity 1 and no phone/address.

## 5. Tests
```bash
pip install pytest pytest-asyncio
pytest -q          # pytest.ini enables pytest-asyncio's auto mode
```
Tests use an in-memory SQLite database and a fake AI provider; no network or real keys are needed.
To run the same suite on PostgreSQL, set `TEST_DATABASE_URL=postgresql://user:pass@host/db` (needs `asyncpg`, already in requirements). **Use an empty test database: the bot's tables in it are dropped and recreated for every test.**

## 6. Troubleshooting
- **"Configuration error(s) in .env"**: a value is missing or still a `<placeholder>`; fix the listed lines.
- **Unknown time zone on Windows**: `pip install tzdata`, or set `TZ` to a valid IANA name.
- **"Bot qo'shildi" message did not arrive**: press Start in the bot once as the main admin, and add the bot to the chat while logged in as the main admin (only his actions are offered a role).
- **"Botga kanalda ... huquqlar yetishmaydi"**: enable Post, Edit and Delete messages for the bot in the posting channel.
- **Nothing is posted**: register at least one posting place (Kanal va guruhlar → Post joyi).
- **Source posts are ignored**: the channel is not registered as a source (`/admin` → Kanal va guruhlar → Manba kanal) or the bot is not its admin.
- **Products stay "pending" or become "failed"**: check the AI status in `/admin`; usually wrong model names or exhausted keys.
- **"Another instance of the bot seems to be running"**: stop the other process (lock port 47400).
- **Photos over 20 MB** cannot be downloaded by the Bot API and will fail AI processing.

## 7. Deploying on Render (free web service)
- The bot runs a tiny HTTP server when Render's `PORT` variable is set: `GET/HEAD /health` (also `/` and `/healthz`) answers `OK`. Locally, where `PORT` is unset, no server starts.
- Build command: `pip install -r requirements.txt`; start command: `python main.py`; health check path: `/health`. A `render.yaml` blueprint is included.
- Enter the `.env` values under **Environment** in the Render dashboard; never upload the `.env` file.
- Free instances sleep after 15 minutes without HTTP traffic. Add an external monitor (UptimeRobot, HetrixTools, cron-job.org) that requests `https://<your-service>.onrender.com/health` every 5 minutes.
- **The free instance's disk is not persistent.** With the default SQLite file (`data/bot.db`) the products, admins and registered chats are lost on every restart or redeploy. Set `DATABASE_URL` to an external PostgreSQL database (section 8).

## 8. PostgreSQL
The bot runs on SQLite (default) or PostgreSQL; the tables are created automatically at startup, so an empty database is all it needs. Nothing is copied over from an existing SQLite file.
- Set `DATABASE_URL` to the provider's connection string. `postgres://` and `postgresql://` are accepted; the bot switches to the `asyncpg` driver, turns `sslmode=...` into `ssl=...` and drops `channel_binding`. Special characters in the password must be URL-encoded (`@` becomes `%40`).
- **Supabase (recommended):** project -> **Connect** -> **Session pooler** (host `...pooler.supabase.com`, port 5432). The direct host is IPv6-only unless you buy the IPv4 add-on, so use the pooler on Render. The transaction pooler (port 6543) also works: prepared statements are switched off.
- **Neon:** works, but the bot reads the database every minute, so the compute never scales to zero and can use up the free plan's compute hours before the month ends.
- Render's own free PostgreSQL expires after 30 days, so it is not suitable.
- Two bot instances starting at the same moment (for example during a deploy) are safe: schema creation is serialised with a PostgreSQL advisory lock.
- If the database is unreachable at startup the bot exits with an error instead of running without data.
