"""Single-file WhatsApp food-order bot server (built by build_bundle.py).
Meta Cloud API only. Env: WHATSAPP_TOKEN, PHONE_NUMBER_ID,
WEBHOOK_VERIFY_TOKEN, PORT, MENU_CSV, TENANTS_JSON.
"""
import json, os, re, time, urllib.request

LALITHA_CSV = 'day,item,aliases,price_usd,available,description\nMonday,Mambhazham Morekozhambu,morekozhambu;morekuzhambu;mambhazham;more kuzhambu ,6.00,yes,\nMonday,Idichakkai Poriyal,idichakkai;idichakka;jackfruit poriyal;tender jackfruit poriyal ,6.00,yes,Tender jackfruit poriyal\nMonday,Olan,olan ,6.00,yes,White pumpkin and yellow pumpkin and red chori cooked in coconut milk\nWednesday,Sambhar,sambar;sambhar ,6.00,yes,\nWednesday,Beans Pattani Poriyal,beans pattani;beans poriyal;pattani poriyal ,6.00,yes,\nWednesday,Mixed Vegetables Kootu,mixed veg kootu;kootu;mixed vegetables kootu ,6.00,yes,Cooked with brown chana\n'

FRIEND2_CSV = 'item,aliases,price_usd,available\nBanana Bread Loaf,banana bread;loaf,9.00,yes\nMasala Khichdi Family Pack,khichdi;family pack,18.00,yes\nFilter Coffee Flask,coffee;filter coffee,7.00,yes\n'

"""Order parser: extracts items/quantities from free-text WhatsApp messages.

MVP uses deterministic alias matching (no LLM hallucinations, code owns pricing).
An LLM hook (parse_with_llm) can be added later behind the same interface:
it must return the same structure and items are re-validated against the menu.
"""
import re

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                "a": 1, "an": 1}

def _qty_before(text, start):
    window = text[max(0, start - 14):start]
    m = re.findall(r"(\d+)\s*(?:x|sets?|plates?|orders?|portions?)?\s*$", window)
    if m:
        return int(m[-1])
    w = re.findall(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|a|an)\s+(?:sets?|plates?|orders?)?\s*$", window)
    if w:
        return NUMBER_WORDS[w[-1]]
    return 1

def expand_day_sets(text, menu, has_dish_items=False):
    """Her customers' PRIMARY pattern: shorthand for a whole day's menu.
    'M' / 'Monday only' / 'Monday menu' -> all Monday dishes, 1x 16 oz each.
    'W' -> Wednesday set. 'all' / 'both' -> both days. A size in the message
    applies to the whole set; 'for 2' / '2 M' sets the quantity.
    Returns [] when the menu has no day field or no strong set trigger —
    a day word next to a named dish ('Monday, 2 olan') is a qualifier,
    not a set order."""
    days = sorted({i["day"] for i in menu if i.get("day")})
    if not days:
        return []
    low = " " + text.lower().strip() + " "
    wants_all = bool(re.search(r"\b(all|both)\b", low)) and not has_dish_items
    strong = (wants_all or re.search(r"\b(menu|only|set)\b", low)
              or re.search(r"\btake\b", low)
              or re.fullmatch(r"[\s\w]*(monday|wednesday|mon|wed|m|w)[\s\w]*", low.strip())
              )
    if not strong:
        return []
    picked = []
    if wants_all:
        picked = days
    else:
        if "Monday" in days and re.search(r"\b(monday|mon|m)\b", low):
            picked.append("Monday")
        if "Wednesday" in days and re.search(r"\b(wednesday|wed|w)\b", low):
            picked.append("Wednesday")
    if not picked:
        return []
    sm = re.search(r"\b(16|24|32)\s*(?:oz)?\b", low)
    size_oz = int(sm.group(1)) if sm else 16
    qty = 1
    qm = re.search(r"\bfor\s+(\d+)\b", low) or re.search(r"\b(\d+)\s*(?:x\s*)?(?:monday|wednesday|mon|wed|m|w|sets?)\b", low)
    if qm:
        qty = int(qm.group(1))
    return [{"name": i["name"], "qty": qty, "size_oz": size_oz,
             "price_cents": i["price_cents"], "available": i.get("available", True),
             "day": i["day"]}
            for i in menu if i.get("day") in picked]

def parse_order(text, menu):
    """menu: list of dicts {name, aliases:[...], price_cents, available:bool}
    Returns {items:[{name, qty, price_cents}], notes, unknown:bool}"""
    low = " " + text.lower().strip() + " "
    found, spans = [], []
    # longest aliases first so 'sambar rice' wins over 'sambar'
    alias_pairs = []
    for item in menu:
        for a in [item["name"]] + item.get("aliases", []):
            alias_pairs.append((a.lower(), item))
    alias_pairs.sort(key=lambda p: -len(p[0]))
    for alias, item in alias_pairs:
        for m in re.finditer(r"\b" + re.escape(alias) + r"\b", low):
            if any(s <= m.start() < e for s, e in spans):
                continue
            spans.append((m.start(), m.end()))
            qty = _qty_before(low, m.start())
            # Size: "32"/"32 oz" just before or after the item wins.
            # Her customers mostly order 16 oz and often state no size -> default 16.
            size_oz = 16
            # Size AFTER the item wins (avoids stealing the previous item's
            # size, e.g. "morekozhambu 32 oz, 1 idichakkai 24").
            sm = re.search(r"\b(16|24|32)\s*(?:oz)?\b", low[m.end():m.end() + 14])
            if not sm:
                before = re.findall(r"\b(16|24|32)\s*oz\b", low[max(0, m.start() - 12):m.start()])
                if before:
                    size_oz = int(before[-1])
            if sm:
                size_oz = int(sm.group(1))
            existing = next((f for f in found if f["name"] == item["name"]
                             and f["size_oz"] == size_oz), None)
            if existing:
                existing["qty"] += qty
            else:
                found.append({"name": item["name"], "qty": qty, "size_oz": size_oz,
                              "price_cents": item["price_cents"],
                              "available": item.get("available", True)})
    notes = ""
    nm = re.search(r"(less spicy|extra spicy|no onion|no garlic|less salt|extra [a-z]+)", low)
    if nm:
        notes = nm.group(1).strip()
    pm = re.search(r"(pickup|pick up|delivery)[^0-9]*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?", low)
    pickup = (pm.group(0).strip() if pm else "")
    intent = "order" if found else "unknown"
    if re.search(r"\b(yes|confirm|ok|okay|sure|👍)\b", low):
        intent = "confirm"
    if re.search(r"\b(cancel|nevermind|never mind|stop)\b", low):
        intent = "cancel"
    # Opting out is NOT an order and NOT a parse failure: "I'll skip this
    # week", "not ordering this week", "out of town", "I'll pass"...
    if (re.search(r"\b(skip|skipping)\b", low)
            or re.search(r"\b(not ordering|no order|can'?t this week|pass this week)\b", low)
            or "out of town" in low or "i'll pass" in low):
        intent = "skip"
    return {"items": found, "notes": notes, "pickup": pickup, "intent": intent}

"""Conversation logic. Code (not the LLM) owns state and pricing.

Flow (per Nandhini, Nov 4, 2026): NO 'reply YES to confirm' step.
Her customers' messages ARE the order — when the bot understands a
message, the order is placed immediately with an order number, and the
customer can change it (send an updated order) or cancel it.
The bot asks a clarifying question ONLY when it is not sure what the
customer means (e.g. part of the order is sold out, or nothing matched).

Coexistence rule: a manual message sent by the owner from the Business
app (smb_message_echo) pauses the bot for that chat for PAUSE_HOURS.
"""
import time

PAUSE_HOURS = 12

class Bot:
    def __init__(self, menu, store):
        self.menu = menu          # list of menu item dicts for today
        self.store = store        # dict-like persistence: store[phone] = state
        self.orders = []          # placed orders (persist in real deploy)
        self._next_id = 1001

    def _paused(self, phone):
        s = self.store.get(phone) or {}
        return s.get("paused_until", 0) > time.time()

    def handle_owner_echo(self, phone):
        """Owner replied manually in the app -> pause bot for this chat."""
        s = self.store.get(phone) or {}
        s["paused_until"] = time.time() + PAUSE_HOURS * 3600
        self.store[phone] = s
        return None

    @staticmethod
    def _line_cents(i):
        # price_cents is per 16 oz; sizes 16/24/32 oz priced proportionally
        # ($6 / $9 / $12 — proportional pricing confirmed Nov 4, 2026).
        return int(round(i["qty"] * i["price_cents"] * i.get("size_oz", 16) / 16))

    def _lines(self, items):
        return "\n".join(
            f'{i["qty"]}x {i["name"]} ({i.get("size_oz", 16)} oz) — ${self._line_cents(i)/100:.2f}'
            for i in items)

    def _active_order(self, phone):
        s = self.store.get(phone) or {}
        oid = s.get("active_order_id")
        return next((o for o in self.orders if o["id"] == oid), None)

    def _place(self, phone, items, notes, pickup, name):
        """Place immediately, or replace the customer's active order
        (a new order message from the same customer = a change)."""
        total = sum(self._line_cents(i) for i in items)
        existing = self._active_order(phone)
        # Keep the chat reply SIMPLE (Nandhini, Nov 4): a thumbs-up reaction
        # (sent by the adapter) + one short line. Full item details live on
        # the owner's dashboard/sheet, not in a long chat repeat-back.
        extra = ""
        if notes:
            extra += f" Noted: {notes}."
        if pickup:
            extra += f" {pickup[0].upper() + pickup[1:]}."
        if existing:
            existing.update({"items": items, "notes": notes, "pickup": pickup,
                             "total_cents": total, "name": name})
            return (f'✅ Order updated #{existing["id"]} — ${total/100:.2f} total.{extra} '
                    f'Reply CANCEL anytime to cancel it.')
        order = {"id": self._next_id, "phone": phone, "name": name,
                 "items": items, "notes": notes, "pickup": pickup,
                 "total_cents": total, "status": "placed"}
        self._next_id += 1
        self.orders.append(order)
        s = self.store.get(phone) or {}
        s["active_order_id"] = order["id"]
        s.pop("pending", None)
        self.store[phone] = s
        return (f'✅ Order placed #{order["id"]} — ${total/100:.2f} total.{extra} '
                f'To change it, just send your updated order, or reply CANCEL.')

    def handle_message(self, phone, text, name=""):
        if self._paused(phone):
            return None  # owner is handling this chat
        s = self.store.get(phone) or {}
        parsed = parse_order(text, self.menu)
        # Day-set shorthand ("I'll take M", "all", "W only, 32 oz") expands
        # to the whole day's dishes and merges with any individual items
        # (which then act as extras on top of the set).
        day_items = expand_day_sets(text, self.menu,
                                    has_dish_items=bool(parsed["items"]))
        if day_items:
            merged = {}
            for i in day_items + parsed["items"]:
                key = (i["name"], i.get("size_oz", 16))
                if key in merged:
                    merged[key]["qty"] += i["qty"]
                else:
                    merged[key] = dict(i)
            parsed["items"] = list(merged.values())
            if parsed["intent"] == "unknown":
                parsed["intent"] = "order"

        if parsed["intent"] == "skip":
            # "I'll skip this week" is a regular opting out, not an order
            # and not a parse failure. Simple friendly ack, nothing placed.
            # If they DO have an order, skipping takes it off.
            active = self._active_order(phone)
            if active:
                self.orders.remove(active)
                s2 = self.store.get(phone) or {}
                s2.pop("active_order_id", None)
                s2.pop("pending", None)
                self.store[phone] = s2
                return (f"👍 Got it — I've taken order #{active['id']} off for "
                        f"this week. See you next week!")
            return "👍 No problem at all — see you next week!"

        if parsed["intent"] == "cancel":
            active = self._active_order(phone)
            if active:
                self.orders.remove(active)
                s2 = self.store.get(phone) or {}
                s2.pop("active_order_id", None)
                s2.pop("pending", None)
                self.store[phone] = s2
                return (f'No problem — order #{active["id"]} is cancelled. '
                        f'Message me anytime to order again.')
            return "No problem — you don't have an active order with me right now."

        # The ONLY confirmation-style step: answering a clarification the
        # bot itself asked because it wasn't sure.
        if parsed["intent"] == "confirm" and s.get("pending"):
            p = s["pending"]
            return self._place(phone, p["items"], p["notes"], p["pickup"], name)

        if parsed["items"]:
            unavailable = [i for i in parsed["items"] if not i["available"]]
            available = [i for i in parsed["items"] if i["available"]]
            if unavailable and not available:
                names = ", ".join(i["name"] for i in unavailable)
                return (f"Sorry, {names} is sold out today. Everything else on "
                        f"the menu is available — would you like something else?")
            if unavailable:
                # Not sure what they want -> ask, don't guess. Nothing is
                # placed until they answer.
                names = ", ".join(i["name"] for i in unavailable)
                s2 = self.store.get(phone) or {}
                s2["pending"] = {"items": available, "notes": parsed["notes"],
                                 "pickup": parsed["pickup"]}
                self.store[phone] = s2
                return (f"Just to be sure — {names} is sold out today. Shall I place "
                        f"the rest of your order?\n{self._lines(available)}\n"
                        f"Reply YES and I'll place it, or tell me what you'd like instead.")
            return self._place(phone, parsed["items"], parsed["notes"],
                               parsed["pickup"], name)

        if parsed["intent"] == "confirm" and self._active_order(phone):
            return "You're all set — your order is already placed ✅"

        # Nothing matched: the bot is not sure -> ask, in her customers' terms.
        return ("Hi! I want to make sure I get your order right. You can reply "
                "with M for Monday's full menu, W for Wednesday's, or 'all' for "
                "both days — or name the dishes, e.g. '2 olan, 1 morekozhambu 32 oz'.")



def send_text(to_phone, body, token=None, phone_number_id=None):
    token = token or os.environ.get("WHATSAPP_TOKEN")
    pn_id = phone_number_id or os.environ.get("PHONE_NUMBER_ID")
    if not token or not pn_id:
        print(f"[DRY-RUN WhatsApp -> {to_phone}] {body}")
        return {"dry_run": True}
    version = os.environ.get("GRAPH_API_VERSION", "v21.0")
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
               "to": to_phone, "type": "text", "text": {"body": body}}
    req = urllib.request.Request(
        f"https://graph.facebook.com/{version}/{pn_id}/messages",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def send_reaction(to_phone, message_id, emoji="👍", token=None, phone_number_id=None):
    """Thumbs-up reaction ON the customer's own message — the lightweight
    acknowledgment (Nandhini, Nov 4): no chat clutter, customer sees their
    order was received. Pairs with the bot's one-line 'Order placed' text."""
    token = token or os.environ.get("WHATSAPP_TOKEN")
    pn_id = phone_number_id or os.environ.get("PHONE_NUMBER_ID")
    if not token or not pn_id:
        print(f"[DRY-RUN WhatsApp reaction {emoji} -> {to_phone} msg {message_id}]")
        return {"dry_run": True}
    version = os.environ.get("GRAPH_API_VERSION", "v21.0")
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
               "to": to_phone, "type": "reaction",
               "reaction": {"message_id": message_id, "emoji": emoji}}
    req = urllib.request.Request(
        f"https://graph.facebook.com/{version}/{pn_id}/messages",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

# ---------------------------------------------------------------- server
import csv, io, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMBEDDED_MENUS = {"lalitha": LALITHA_CSV, "friend2": FRIEND2_CSV}

def load_menu(path="lalitha"):
    if path in EMBEDDED_MENUS:
        text = EMBEDDED_MENUS[path]
    elif path.startswith("embedded:"):
        text = EMBEDDED_MENUS[path.split(":", 1)[1]]
    else:
        with open(path) as f:
            text = f.read()
    menu = []
    for row in csv.DictReader(io.StringIO(text)):
        item = {"name": row["item"],
                "aliases": [a.strip() for a in row["aliases"].split(";") if a.strip()],
                "price_cents": int(round(float(row["price_usd"]) * 100)),
                "available": row["available"].strip().lower() != "no"}
        if row.get("day") and row["day"].strip():
            item["day"] = row["day"].strip()
        menu.append(item)
    return menu

STORE = {}
BOT = Bot(load_menu(os.environ.get("MENU_CSV", "lalitha")), STORE)

_TENANTS = {}
try:
    _TENANTS = json.loads(os.environ.get("TENANTS_JSON", "{}"))
except Exception:
    _TENANTS = {}
if not _TENANTS and os.path.exists("tenants.json"):
    try:
        with open("tenants.json") as f:
            _TENANTS = json.load(f)
    except Exception:
        _TENANTS = {}
_BOTS = {}

def get_bot(tenant_id):
    if not tenant_id or tenant_id not in _TENANTS:
        return BOT, None, None
    cfg = _TENANTS[tenant_id]
    if tenant_id not in _BOTS:
        _BOTS[tenant_id] = Bot(load_menu(cfg["menu_csv"]), {})
    token = os.environ.get(cfg["token_env"]) if cfg.get("token_env") else None
    return _BOTS[tenant_id], tenant_id, token

def extract_events(payload):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            tenant_id = val.get("metadata", {}).get("phone_number_id", "")
            contacts = {c.get("wa_id"): c.get("profile", {}).get("name", "")
                        for c in val.get("contacts", [])}
            for msg in val.get("messages", []):
                if msg.get("type") == "text":
                    yield ("message", tenant_id, msg["from"], msg["text"]["body"],
                           contacts.get(msg["from"], ""), msg.get("id", ""))
            for echo in val.get("message_echoes", []) or val.get("smb_message_echoes", []):
                yield ("echo", tenant_id, echo.get("to", ""), "", "", "")

def render_dashboard():
    import html as H
    bots = [("Order Bot", BOT)] + [(tid, b) for tid, b in _BOTS.items()]
    out = ["<!doctype html><meta charset=utf-8><meta name=viewport "
           "content='width=device-width,initial-scale=1'>"
           "<meta http-equiv=refresh content=15>"
           "<title>Orders Dashboard</title><style>"
           "body{font-family:system-ui;margin:16px;color:#111}"
           "table{border-collapse:collapse;width:100%;font-size:14px}"
           "td,th{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}"
           ".muted{color:#666;font-size:13px}</style>"]
    for label, bot in bots:
        orders = [o for o in bot.orders if o.get("status") == "placed"]
        revenue = sum(o["total_cents"] for o in orders) / 100
        day_of = {i["name"]: i.get("day", "") for i in bot.menu}
        cook, pack = {}, {}
        for o in orders:
            for i in o["items"]:
                d = i.get("day") or day_of.get(i["name"], "")
                key = (d, i["name"])
                cook[key] = cook.get(key, 0) + i["qty"] * i.get("size_oz", 16)
                pack[i.get("size_oz", 16)] = pack.get(i.get("size_oz", 16), 0) + i["qty"]
        out.append(f"<h2>{H.escape(label)}</h2>")
        out.append(f"<p><b>{len(orders)} orders \u00b7 ${revenue:.2f}</b> "
                   f"<span class=muted>packing: "
                   + ", ".join(f"{n}x {s} oz" for s, n in sorted(pack.items()))
                   + "</span></p>")
        if cook:
            out.append("<p class=muted>To cook: " + " \u00b7 ".join(
                f"{H.escape(d + ' ' if d else '')}{H.escape(n)}: {oz} oz"
                for (d, n), oz in sorted(cook.items())) + "</p>")
        out.append("<table><tr><th>#</th><th>Customer</th><th>Items</th><th>Total</th></tr>")
        for o in sorted(orders, key=lambda x: -x["id"]):
            items = "<br>".join(
                f'{i["qty"]}x {H.escape(i["name"])} ({i.get("size_oz", 16)} oz)'
                + (f' <span class=muted>[{H.escape(i.get("day") or day_of.get(i["name"], ""))}]</span>'
                   if (i.get("day") or day_of.get(i["name"])) else "")
                for i in o["items"])
            if o.get("notes"):
                items += f'<br><span class=muted>Note: {H.escape(o["notes"])}</span>'
            out.append(f"<tr><td>#{o['id']}</td>"
                       f"<td>{H.escape(o.get('name') or '')}<br><span class=muted>{H.escape(o['phone'])}</span></td>"
                       f"<td>{items}</td><td>${o['total_cents']/100:.2f}</td></tr>")
        out.append("</table>")
    return "".join(out)

def _deliver(phone, reply, msg_id, tid, tok):
    try:
        if msg_id and reply.startswith("\u2705"):
            try:
                send_reaction(phone, msg_id, "\U0001F44D",
                              token=tok, phone_number_id=tid)
            except Exception as e:
                print(f"[send] reaction failed: {e}", flush=True)
        send_text(phone, reply, token=tok, phone_number_id=tid)
        print(f"[send] reply sent to {phone}", flush=True)
    except Exception as e:
        print(f"[send] text failed: {e}", flush=True)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404); self.end_headers(); return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            payload = {}
        jobs = []
        for kind, tenant_id, phone, text, name, msg_id in extract_events(payload):
            bot, tid, tok = get_bot(tenant_id)
            if kind == "echo" and phone:
                bot.handle_owner_echo(phone)
            elif kind == "message":
                print(f"[webhook] from={phone} text={text!r}", flush=True)
                reply = bot.handle_message(phone, text, name)
                if reply:
                    jobs.append((phone, reply, msg_id, tid, tok))
        # Fast 200 first; sends happen in the background so a slow
        # Graph call can never block or time out the webhook.
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        for phone, reply, msg_id, tid, tok in jobs:
            threading.Thread(target=_deliver,
                             args=(phone, reply, msg_id, tid, tok),
                             daemon=True).start()
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/dashboard":
            key = os.environ.get("DASHBOARD_KEY") or os.environ.get("WEBHOOK_VERIFY_TOKEN", "")
            if key and q.get("k", [""])[0] == key:
                body = render_dashboard().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers(); self.wfile.write(body)
                return
            self.send_response(403); self.end_headers(); return
        if q.get("hub.mode", [""])[0] == "subscribe":
            expected = os.environ.get("WEBHOOK_VERIFY_TOKEN", "")
            if expected and q.get("hub.verify_token", [""])[0] == expected:
                challenge = q.get("hub.challenge", [""])[0]
                self.send_response(200); self.end_headers()
                self.wfile.write(challenge.encode())
                return
            self.send_response(403); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(b"food-bot up")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
