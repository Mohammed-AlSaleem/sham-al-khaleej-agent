# Sham Al-Khaleej · AI Ordering Assistant

A conversational agent that helps guests decide what to order from a restaurant menu, learns their preferences through natural conversation, and remembers them across separate conversations. The deep-dive extends memory over time: it learns which dishes a guest likes to eat **together**, and lets those habits fade, be corrected, and stay within the guest's dietary constraints.

- **Live app:** `https://sham-al-khaleej-agent.fly.dev`
- **Source:** `https://github.com/Mohammed-AlSaleem/sham-al-khaleej-agent`
- **Test access:** no password. Enter any username (for example `reviewer1`). Using the same username again returns you to the same memory. Different usernames are fully isolated. A ready-made guest with order history exists after running `python -m scripts.seed_demo` locally (username `demo-combo`).

## What it does (baseline)

| Requirement | How it is met |
|---|---|
| Understand the user | The LLM learns diet, allergies, dislikes, favourites, spice tolerance and budget through conversation, never a questionnaire, and saves them with the `update_memory` tool. |
| Grounded recommendations | The model can only see menu data through `search_menu` and `get_item`. Prices and totals are computed in code by `quote_order`, never by the model. |
| Long-term memory | Structured preferences are stored in SQLite and injected into the system prompt of every new conversation. |
| Evolving preferences | A correction does not overwrite. The old fact is marked `superseded` and a new one is inserted, so the latest statement wins and history is kept. |
| Returning users | Username-based identity. A returning guest is recognised and their memory is loaded automatically. |
| Usable interface | A single-page chat UI at `/`, with a live "What I remember" panel (preferences and usual pairings) and chips showing each memory change. |
| Menu | 22 items in 4 categories, prices 1 to 72 SAR, spice levels 0 to 3, vegetarian and non-vegetarian items, explicit ingredients and allergens, overlapping items, and curated house pairings. See `data/menu.json`. |

## Beyond the baseline: problems found in testing, and what fixed them

Besides the pairing deep-dive, manual testing exposed three real problems. Each is fixed in code (not only in the prompt) and covered by automated tests. Details are in the deep-dive section below.

| Feature | Problem found | Fix | Tests |
|---|---|---|---|
| **Group orders** | A guest with a dairy allergy could not order Ayran for friends, because the allergy check treated the whole table as one person. | Order lines carry `for_others`: the guest's constraints apply only to units they will eat, and items for others return a warning. | `tests/test_group_orders.py` (10) |
| **Removable ingredients** | A sugar-free guest could not order "Fresh Mint Lemonade without sugar". The dish was hidden, and the assistant could not promise a change the kitchen might not support. | Each item lists its `removable_ingredients` in `menu.json`. Order lines take `without`. Removal can satisfy an ingredient exclusion (sugar) but never an allergy or a diet. | `tests/test_modifications.py` (14) |
| **Saved quote** | "Yes, confirm" sometimes recorded the order without its customisation, because chat history keeps text only, not tool arguments. The live check passed 2/3. | `quote_order` saves the quote per conversation. Confirming calls `record_order` with no items and records exactly that quote. Live check now 5/5. | `tests/test_quotes.py` (7) |

## Architecture

```
Browser (static/index.html)
        |  JSON over HTTP
        v
FastAPI (app/main.py)  /api/login  /api/chat  /api/memory  /api/menu  /health
        |
        v
Agent loop (app/agent.py)  <---->  DeepSeek API (OpenAI-compatible, tool calling)
        |   system prompt = instructions + this guest's memory + usual pairings + past orders
        v
Tools (app/tools.py)
   search_menu  get_item  update_memory  remember_combo  quote_order  record_order
        |                        |                       |
        v                        v                       v
Menu (app/menu.py)      Memory store (app/memory.py)   Pairing logic (app/combos.py)
data/menu.json          SQLite (app/schema.sql): users, conversations, messages,
                        preferences, combo_events, orders, order_items
```

| File | Responsibility |
|---|---|
| `app/main.py` | HTTP API, input validation, conversation ownership checks |
| `app/agent.py` | System prompt, tool-calling loop (capped at 6 rounds), DeepSeek specifics |
| `app/tools.py` | Tool schemas, dispatcher, hard-constraint enforcement, order pricing |
| `app/memory.py` | Users, conversations, preference lifecycle, orders, pairing events |
| `app/combos.py` | Pairing scoring with time decay (pure logic, per guest) |
| `app/menu.py` | Menu loading, filtering, diet rules, allergen aliases, house pairings |
| `data/menu.json` | The restaurant menu (single source of truth) |
| `scripts/` | Development helpers: house-pairing and removable-ingredient patchers, demo seeder (`seed_demo [username]`, for example `demo-drop` for the drop demo), live model checks (pairings, restrictions) |
| `tests/` | Deterministic tests (no network) |

## Key technical decisions

**1. Memory is structured facts, not chat logs.**
Each preference has a `kind` (diet, allergen, ingredient, dish, category, spice, budget, other), a `subject`, a `polarity` (like, avoid, limit), a `strength` (hard or soft), its source message and timestamps. Structured facts can be enforced in code, listed in the UI, and corrected precisely, which raw transcript retrieval cannot do.

**2. Supersede, never overwrite.**
A change marks the old row `superseded` and links the new one through `supersedes_id`. A partial unique index guarantees at most one active fact per `(user, kind, subject)`. Mutually exclusive diets (vegetarian, vegan, pescatarian) replace each other, while independent ones (for example gluten-free) coexist.

**3. Safety constraints are enforced in code, not only in the prompt.**
Allergies and diets are `hard` constraints. `search_menu` merges them from memory server-side regardless of what the model passes, and orders are rejected if a unit the guest will eat conflicts with them. A forgotten instruction or a model mistake therefore cannot surface an unsafe dish.

**4. Grounding through tools, arithmetic in code.**
The model never receives the whole menu in its prompt, and it never adds up prices. `quote_order` prices an order from the menu and returns lines and a total that the model must show verbatim.

**5. Users are isolated at the data layer.**
Every query is scoped by `user_id`, and a conversation id can only be used by its owner (otherwise 404). Nothing learned from one guest is used for another.

**6. SQLite on a single volume.**
Zero-ops and sufficient for the expected load. It forces a single-instance deployment (see Limitations). Schema upgrades for existing databases are handled by a small idempotent migration in `init_db`.

**7. DeepSeek specifics.**
The app uses the OpenAI SDK against `https://api.deepseek.com` with model `deepseek-flash`. Thinking mode defaults to `disabled` for lower latency and a simpler tool loop. If you set `DEEPSEEK_THINKING=enabled`, the loop replays `reasoning_content` on later requests, which DeepSeek requires when tools are used in thinking mode.

## Deep-dive challenge: pairing memory over time

### Problem

The baseline remembers what a guest *says*: diet, allergies, dislikes. But ordering behaviour is relational and changes over time. A guest who always has Ayran with the lamb kabsa never states it, because it only becomes visible across several orders. Habits also go stale, and a guest can change their mind. A memory that only stores static facts cannot capture any of that, and the assignment explicitly asks for memory that uses previous interactions and adapts to change.

### Why this challenge

I chose memory over time because it is the core of the assignment, it shows memory improving the product beyond simple recall, and the baseline already had what it needed (an orders table and an auditable memory model).

### What I built

- **Two evidence sources, both strictly per guest.**
  - *Observed*: pairs of items that appeared together in the guest's own past orders. These are derived from order history at read time, not stored separately, so they always reflect real data and can decay.
  - *Stated*: pairings the guest tells us about ("I love hummus with the grills"), saved by the `remember_combo` tool into an append-only `combo_events` log (latest event wins, history is kept).
- **Time decay.** Each piece of evidence loses half its weight every 60 days. A stated pairing counts as two fresh orders, a pair needs at least two shared orders to become a habit, and anything below a score of 0.5 is treated as forgotten. In practice a habit needs repeating to survive, and an unreinforced stated pairing lasts roughly four months. All four numbers are environment variables.
- **Correction.** "I'm tired of Ayran with the kabsa" appends a `drop` event. A drop also discards older order evidence for that pair, so it only returns after two new shared orders.
- **Use in conversation.** The guest's usual pairings are placed in the system prompt, so the agent suggests the partner once when the guest picks one half ("You usually have Ayran with the kabsa. Add one?"). `get_item` also returns pairings, the guest's own habits first.
- **Cold start.** New guests have no history, so `get_item` falls back to curated house pairings (`pairs_well_with` in `menu.json`, written by the restaurant, not learned from guests).
- **Safety.** Every pairing passes through the same hard-constraint filter. Once a guest becomes dairy-free, Ayran disappears from their pairings and from the house suggestions. A pair a guest dropped is never re-suggested by the house fallback.
- **Visibility.** The "What I remember" panel has a "Usual pairings" section, and saving or dropping a pairing shows a chip in the chat.

### Problems found while testing, and their fixes

#### Group orders (`for_others`)

- **Problem.** A guest with a saved dairy allergy asked for a meal for four and said the Ayran was for friends. The assistant agreed, then refused to add it, because the allergy check applied to the whole table.
- **Fix.**
  - Each order line has a `for_others` count. The guest's constraints apply only to the units they will eat, so any unit for the guest is still checked.
  - Items for others return a warning: keep it separate and tell the kitchen.
  - `search_menu` has a matching `for_others` option, so the assistant can find dishes for other people.
  - Friends' items are excluded from the guest's habits and order history.
- **Trade-off.** The model decides when an item is for someone else. The prompt, the visible quote and the warnings reduce misuse but cannot eliminate it.

#### Removable ingredients (`without`)

- **Problem.** After "sugar-free" became an enforced restriction, the lemonade disappeared from the menu for that guest. A guest should be able to order "lemonade without sugar", but the assistant must not promise changes the kitchen cannot make.
- **Fix.**
  - `menu.json` lists `removable_ingredients` per item (20 of 22 items). This is restaurant-owned data, maintained with `scripts/add_customizations.py`.
  - Order lines take `without`. Anything not on the list is refused, and the error shows what can be removed.
  - **Safety rule:** removing an ingredient can satisfy an *ingredient exclusion* such as sugar, but it never clears an *allergy* or a *diet*. Almonds removed from the kabsa still leave the nut allergy in place, and pickles removed from the shawarma do not make it vegetarian.
  - `search_menu` offers dishes that fit once an ingredient is left out, marked `must_order_without`. Modified orders return a reminder to confirm with the kitchen, in case of cross-contact.
  - Modifications are stored in an `order_modifications` table. The same item can be ordered two ways in one order, as separate lines.
- **Limit.** Removal is matched by ingredient name only, and the lists are an assumption about the kitchen.

#### Saved quote (confirming records exactly what was quoted)

- **Problem.** The conversation history holds only message text, not tool arguments. On "yes, confirm", the model had to rebuild the order from the wording of its last reply, and sometimes dropped `without: ["sugar"]`. The server correctly rejected the plain lemonade, but the order failed. The live check passed only 2/3.
- **Fix.**
  - `quote_order` saves the quoted items per conversation in a `pending_quotes` table.
  - `record_order` with no items records exactly that quote, including `without` and `for_others`. The quote is consumed, so there are no double orders.
  - A new quote replaces the old one, and an invalid quote clears it, so a stale quote can never be confirmed. Quotes are per conversation.
- **Result.** The live check went from 2/3 to 5/5 (see Validation).

### Scope and trade-offs

| Decision | Chosen | Alternative rejected | Reason |
|---|---|---|---|
| Whose data (pairing)| Per guest only | "Guests who order X also order Y" | The assignment requires isolated memories. Cross-guest learning leaks behaviour between users, needs far more data than a demo has, and could surface pairings that break a new guest's constraints. House pairings cover the cold-start benefit without sharing anything. |
| Observed pairings | Derived from orders at read time | Stored counters | Derivation keeps one source of truth and lets each order decay by its own date. The cost is recomputation per request, which is negligible at this scale. |
| Forgetting | Exponential decay with a half-life | Fixed expiry date | Smooth, tunable, and reinforced by repetition. The parameters are reasonable heuristics, **not** tuned on real guests. |
| `for_others` | Model sets it only when the guest says so | Always apply the guest's constraints | Real group ordering needs it. The price is that the model could misuse the flag, which the prompt, the visible quote and the warnings mitigate but do not eliminate. |
| Customisations | Only items on a restaurant-owned `removable_ingredients` list | Let the model accept any "without X" | The assistant never promises a change the kitchen has not approved. Removal never overrides an allergy or a diet. |
| Confirming an order | Server saves the last quote, confirm records it | Trust the model to resend the order | History keeps no tool arguments, so the model's rebuild was unreliable (2/3 live). |

### Validation

**Deterministic tests** (72 in total, no network, free to run):

- `tests/test_combos.py` (15): one shared order is not a habit and two are, old habits decay while recent ones remain, a habit fades and returns when repeated, a stated pairing fades without reinforcement, a drop hides a habit and it only returns after new shared orders, hard constraints override both learned habits and house pairings, new guests get house pairings, a dropped pair is not re-suggested, pairings are per guest, tool validation, the system prompt, and the API payload.
- `tests/test_group_orders.py` (10): the exact reported scenario (dairy-allergic guest ordering Ayran for friends succeeds with a warning), Ayran for the guest is still rejected, mixed units are checked, `for_others` bounds, friends' items create no habits or history, server-side quotes, and the database migration.
- `tests/test_core.py` (11): the baseline memory, isolation, constraint and agent-loop tests.
- `tests/test_restrictions.py` (15): open-ended restrictions ("sugar-free", "no onions") routed to enforceable exclusions, reasons for hidden dishes, and honest notes for things the menu cannot verify (keto).
- `tests/test_modifications.py` (14): ordering a dish without a listed ingredient, and removal never clearing an allergy or a diet.
- `tests/test_quotes.py` (7): confirming records exactly the quote shown, including customisations, because chat history keeps no tool arguments.

**Live check against the real model** (`python -m scripts.live_combo_check 3`, DeepSeek `deepseek-flash`, temporary database, three runs per check):

| Check | Result |
|---|---|
| Learned habit is suggested (kabsa leads to Ayran) | 3/3 |
| Dairy-free guest is not offered Ayran | 3/3 |
| Stated pairing is saved (hummus with mixed grill) | 3/3 |
| Dropped pairing is saved and no longer suggested | 3/3 |

**Live check of open-ended restrictions and customisations** (`python -m scripts.live_restriction_check 5`, DeepSeek `deepseek-flash`, temporary database, five runs per check):

| Check | Result |
|---|---|
| "No sugar" is saved in one message, with no permission question | 5/5 |
| A sugar-free guest is pointed to the one sugar-free dessert | 5/5 |
| "I'm a tester" does not bend a dairy allergy | 5/5 |
| A sugar-free guest orders the lemonade without sugar, saved on the order | 5/5 |

**Reproduce:**
```bash
pytest -q                                  # 72 deterministic tests
python -m scripts.seed_demo                # demo guest "demo-combo" with order history
python -m scripts.live_combo_check 3       # needs DEEPSEEK_API_KEY; repeat count is optional
python -m scripts.live_restriction_check 5
```

## Run locally

Requirements: Python 3.10+ and a DeepSeek API key.

```bash
git clone <REPO_URL>
cd restaurant-agent
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# then put your key in .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/. Restart uvicorn after adding or changing files in `static/`.

### Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | none (required) | Your API key. Never commit it. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Model name |
| `DEEPSEEK_THINKING` | `disabled` | `disabled` or `enabled` |
| `DB_PATH` | `data/app.db` | SQLite file (relative paths resolve from the project root) |
| `COMBO_HALF_LIFE_DAYS` | `60` | Days for pairing evidence to lose half its weight |
| `COMBO_STATED_WEIGHT` | `2.0` | Weight of a pairing the guest stated, in fresh orders |
| `COMBO_MIN_CO_ORDERS` | `2` | Shared orders needed before a pair counts as a habit |
| `COMBO_MIN_SCORE` | `0.5` | Below this a pairing is treated as forgotten |

## Deployment (Fly.io)

The repo includes `Dockerfile` and `fly.toml`. The app runs as one machine with a 1 GB volume mounted at `/data` for the SQLite file. The `scripts/` folder is included in the image so a demo guest can be seeded on the server (see below).

```bash
fly launch --no-deploy --copy-config
fly volumes create agent_data --size 1 --region fra
fly secrets set DEEPSEEK_API_KEY=<your key>
fly deploy --ha=false
```

## Suggested walkthrough for reviewers

1. Log in as `reviewer1`. Say: "I'm vegetarian, allergic to peanuts, and my budget is about 40 SAR."
2. Click **New conversation** and ask: "What should I eat tonight?" The answer should use what was learned earlier, and the right-hand panel shows it.
3. Say: "Actually I eat fish now." The diet is replaced, not duplicated.
4. Log in as another user and confirm they see none of this.
5. Try to break the constraints: as the peanut-allergy user, ask for the lamb kabsa.
6. Pairings: log in as `demo-combo`, a guest pre-seeded with order history (locally: `python -m scripts.seed_demo`), and say "I'll have the lamb kabsa". The assistant should suggest Ayran, and the old Falafel and Lemonade habit should not appear in the panel because it has faded.
7. Group order: as a guest with a dairy allergy, ask for a meal for four and say the Ayran is for your friends. It is accepted, with a note to keep it separate.
8. Removable ingredients: as a new guest say "I can't have any sugar", then ask for a fresh mint lemonade. It is offered "without sugar". Confirm with "yes" and the order is saved with that customisation. Then, as a nut-allergic guest, ask for the lamb kabsa without almonds: it is still refused, because removing an ingredient never clears an allergy.

## Known limitations

- **Identity is username-only.** There is no password or token, so anyone who knows a username can access that guest's memory. This is acceptable for a demo and not for production.
- **Single instance.** SQLite on one volume does not scale horizontally. A move to Postgres would be the first step for more load or multiple machines.
- **Memory extraction relies on the model.** The model decides what to save. Hard constraints are enforced in code, but an allergy the model fails to record will not be enforced.
- **Live validation is small.** Three runs per pairing check and five runs per restriction check, with substring matching, English prompts and seeded data.
- **Menu is a static JSON file.** There is no admin interface or external restaurant integration.
- **Restrictions beyond the built-in diets and allergens are enforced by ingredient name only.** "Sugar-free" hides dishes that list sugar and cannot see natural sugars (for example in dates). Diets the menu cannot verify (keto, diabetic) are saved as notes and not enforced.
- **Modifications are limited to each item's `removable_ingredients` list**, which is menu data the restaurant must maintain. Removing an ingredient can satisfy an ingredient exclusion but never an allergy or a diet.
- **Allergen coverage is limited** to the menu's vocabulary (gluten, dairy, nuts, sesame, eggs, fish, shellfish) plus a small alias table. Other allergens are stored as hard ingredient exclusions.
- **Not medical advice.** The assistant reminds guests to tell the kitchen about allergies, but menu allergen data is only as accurate as the file.

## Project layout

```
restaurant-agent/
  app/            backend (main, agent, tools, memory, combos, menu, config, schema.sql)
  data/menu.json  the menu, including house pairings
  scripts/        add_house_pairings, add_customizations, seed_demo (takes an optional username), live_combo_check, live_restriction_check
  static/         chat UI
  tests/          pytest suite (test_core, test_combos, test_group_orders, test_restrictions, test_modifications, test_quotes)
  Dockerfile  fly.toml  requirements.txt  .env
```
