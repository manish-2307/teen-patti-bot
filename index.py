"""Teen Patti web API (FastAPI) - state lives in Upstash Redis so it works on Vercel."""
import json
import os
import random
import string
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()
SUITS = "♠♥♦♣"
RL = {11: "J", 12: "Q", 13: "K", 14: "A"}
MAX_PLAYERS = 6
TURN_SECS = 30

# ---------------- storage ----------------
URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
TOK = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")


def redis(*cmd):
    req = urllib.request.Request(
        URL, json.dumps(cmd).encode(),
        {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=8))["result"]


def load(code):
    s = redis("GET", "tp:" + code)
    return json.loads(s) if s else None


def stamp(g):
    k = [g["turn"], g["side"], g["hand_no"], g["phase"], len(active(g))]
    if g.get("tk") != k:
        g["tk"], g["turn_at"] = k, time.time()


def save(code, g):
    stamp(g)
    redis("SET", "tp:" + code, json.dumps(g), "EX", 86400)


@app.get("/", include_in_schema=False)
async def home():
    here = Path(__file__).resolve().parent
    for p in (here.parent / "public" / "index.html", Path("public/index.html"), here / "index.html"):
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>public/index.html is missing from the repo</h1>", status_code=500)


def err(msg, status=400):
    return JSONResponse({"error": msg}, status_code=status)


# ---------------- hand ranking (same as the bot) ----------------
def cs(n):
    r = 2 + n % 13
    return f"{RL.get(r, r)}{SUITS[n // 13]}"


def rank(h):
    v = sorted((2 + n % 13 for n in h), reverse=True)
    color = len({n // 13 for n in h}) == 1
    q = 0
    if v == [14, 13, 12]:
        q = 15
    elif v == [14, 3, 2]:
        q = 14
    elif v[0] - v[1] == 1 and v[1] - v[2] == 1:
        q = v[0]
    if v[0] == v[2]:
        return (5, [v[0]], "Trail")
    if q and color:
        return (4, [q], "Pure sequence")
    if q:
        return (3, [q], "Sequence")
    if color:
        return (2, v, "Color")
    if v[0] == v[1]:
        return (1, [v[0], v[2]], "Pair")
    if v[1] == v[2]:
        return (1, [v[1], v[0]], "Pair")
    return (0, v, "High card")


def cmp(a, b):
    x, y = rank(a), rank(b)
    if (x[0], x[1]) == (y[0], y[1]):
        return 0
    return 1 if (x[0], x[1]) > (y[0], y[1]) else -1


# ---------------- game rules on a plain dict ----------------
def new_game(host_id, boot, mode="classic"):
    return dict(mode=mode, chat=[], turn_at=0, boot=boot, host=host_id, players=[], phase="lobby", hand_no=0, dealer=-1,
                turn=0, stake=boot, pot=0, side=None, log=[], result=None, shown=[], winner=None)


def add(g, pid, name):
    play = g["phase"] == "play"
    name = name[:16] or "Player"
    for p in g["players"]:
        if p["id"] == pid:
            if not p.get("left"):
                return False
            p.update(left=False, wait=play, packed=play, name=name)
            return True
    if len(g["players"]) >= MAX_PLAYERS:
        return False
    g["players"].append(dict(id=pid, name=name, chips=g["boot"] * 100, bet=0, packed=play,
                             seen=False, hand=[], wait=play, left=False))
    return True


def active(g):
    return [p for p in g["players"] if not p["packed"]]


def nxt(g, i, step=1):
    n, j = len(g["players"]), i
    while True:
        j = (j + step) % n
        if not g["players"][j]["packed"] or j == i:
            return j


def cost(g, p, raise_):
    if p["seen"]:
        return g["stake"] * (4 if raise_ else 2)
    return g["stake"] * (2 if raise_ else 1)


def pay(g, p, a):
    a = min(a, p["chips"])
    p["chips"] -= a
    p["bet"] += a
    g["pot"] += a
    return a


def say(g, t):
    g["log"] = (g["log"] + [t])[-5:]


def purge(g):
    P, d = g["players"], g["dealer"]
    g["dealer"] = sum(1 for p in P[:d + 1] if not p.get("left")) - 1
    g["players"] = [p for p in P if not p.get("left")]
    if g["players"] and g["host"] not in [p["id"] for p in g["players"]]:
        g["host"] = g["players"][0]["id"]


def deal(g):
    purge(g)
    d = list(range(52))
    random.shuffle(d)
    g.update(winner=None, pot=0, stake=g["boot"], side=None, result=None, shown=[], log=[])
    for p in g["players"]:
        if p["chips"] < g["boot"]:
            p["chips"] = g["boot"] * 100  # rebuy
        p["hand"] = [d.pop() for _ in range(3)]
        p["bet"], p["packed"], p["seen"], p["wait"] = 0, False, False, False
        pay(g, p, g["boot"])
    g["hand_no"] += 1
    g["dealer"] = (g["dealer"] + 1) % len(g["players"])
    g["turn"] = (g["dealer"] + 1) % len(g["players"])
    g["phase"] = "play"
    say(g, f"Hand {g['hand_no']} dealt. Boot {g['boot']} each.")


def only_left(g):
    return next(k for k, x in enumerate(g["players"]) if not x["packed"])


def finish(g, w, text, shown=()):
    p = g["players"][w]
    p["chips"] += g["pot"]
    g["result"] = f"{p['name']} wins {g['pot']} ({text})"
    g.update(winner=w, pot=0, shown=list(shown), phase="over", side=None)
    say(g, g["result"])


def chaal(g, raise_):
    p = g["players"][g["turn"]]
    a = pay(g, p, cost(g, p, raise_))
    if raise_:
        g["stake"] *= 2
    say(g, f"{p['name']} {'raised' if raise_ else 'played'} ({'seen' if p['seen'] else 'blind'}) {a}")
    g["turn"] = nxt(g, g["turn"])


def pack(g):
    p = g["players"][g["turn"]]
    p["packed"] = True
    say(g, f"{p['name']} packed.")
    if len(active(g)) == 1:
        finish(g, only_left(g), "everyone else packed")
    else:
        g["turn"] = nxt(g, g["turn"])


def show(g):
    P, i = g["players"], g["turn"]
    pay(g, P[i], cost(g, P[i], False))
    j = next(k for k, x in enumerate(P) if not x["packed"] and k != i)
    c = cmpg(g, P[i]["hand"], P[j]["hand"])
    w, l = (i, j) if c > 0 else (j, i)  # tie: the player who asked for the show loses
    note = " (tie goes to the opponent)" if c == 0 else ""
    finish(g, w, f"{rank(P[w]['hand'])[2]} vs {rank(P[l]['hand'])[2]}{note}", [i, j])


def ask_side(g):
    P, i = g["players"], g["turn"]
    t = nxt(g, i, -1)
    pay(g, P[i], cost(g, P[i], False))
    g["side"] = [i, t]
    say(g, f"{P[i]['name']} asks {P[t]['name']} for a side show.")


def answer_side(g, ok):
    f, t = g["side"]
    g["side"] = None
    a, b = g["players"][f], g["players"][t]
    if ok:
        loser = b if cmpg(g, a["hand"], b["hand"]) > 0 else a  # tie: requester packs
        loser["packed"] = True
        say(g, f"Side show: {loser['name']} packs.")
        if len(active(g)) == 1:
            finish(g, only_left(g), "side show", [f, t])
            return
    else:
        say(g, f"{b['name']} declined the side show.")
    g["turn"] = nxt(g, f)


def cmpg(g, a, b):
    c = cmp(a, b)
    return -c if g.get("mode") == "muflis" else c  # muflis: lowest hand wins


def find(g, pid):
    return next((i for i, p in enumerate(g["players"]) if p["id"] == pid and not p.get("left")), None)


def leave(g, me):
    P = g["players"]
    p = P[me]
    p["left"] = True
    nl = [x for x in P if not x["left"]]
    if g["host"] == p["id"] and nl:
        g["host"] = nl[0]["id"]
    if g["phase"] == "lobby":
        purge(g)
        return
    if g["phase"] != "play":
        return
    if g["side"] and me in g["side"]:
        g["side"] = None
    if p["packed"]:
        return
    if g["turn"] == me:
        pack(g)
    else:
        p["packed"] = True
        say(g, f"{p['name']} left.")
        if len(active(g)) == 1:
            finish(g, only_left(g), "everyone else left")


def tick(g):
    """Turn timer: runs lazily on every request. Out of time = pack (or decline a side show)."""
    if g["phase"] != "play" or time.time() - g.get("turn_at", 0) < TURN_SECS:
        return False
    if g["side"]:
        answer_side(g, False)
    else:
        say(g, f"{g['players'][g['turn']]['name']} timed out.")
        pack(g)
    return True


BOTS = ["Bot Ravi", "Bot Priya", "Bot Amit"]
WHAT = {"Trail": "three cards of the same rank, the best hand",
        "Pure sequence": "three consecutive cards of one suit",
        "Sequence": "three consecutive cards of mixed suits",
        "Color": "three cards of one suit",
        "Pair": "two cards of the same rank",
        "High card": "no combination, so the highest card counts"}
ADVICE = ["Weak. Pack if the stake is rising, or gamble with a Show if only one opponent is left.",
          "Medium. Keep playing while the stake is small, but pack if it climbs high.",
          "Strong. Keep playing and think about a Raise.",
          "Very strong. Raise or play on, and Show when two players are left."]


def strength(g, h):
    c = rank(h)[0]
    if g.get("mode") == "muflis":
        c = 5 - c
    return min(c, 3) if c < 3 else 3


def coach_tip(g, p):
    if g["phase"] != "play" or p["packed"]:
        return ""
    if not p["seen"]:
        return "You are playing blind, so every bet costs half. Press See cards to look at your hand."
    r = rank(p["hand"])[2]
    low = "In Muflis the lowest hand wins, so weak hands are good. " if g.get("mode") == "muflis" else ""
    return f"Your hand: {r}, {WHAT[r]}. {low}{ADVICE[strength(g, p['hand'])]}"


def bot_move(g):
    """Bots move lazily, 1.5s after their turn starts, so humans can follow the game."""
    if g["phase"] != "play" or time.time() - g.get("turn_at", 0) < 1.5:
        return False
    P = g["players"]
    if g["side"]:
        t = P[g["side"][1]]
        if not t.get("bot"):
            return False
        answer_side(g, strength(g, t["hand"]) >= 2 or random.random() < .3)
        return True
    p = P[g["turn"]]
    if not p.get("bot"):
        return False
    st, two, r = strength(g, p["hand"]), len(active(g)) == 2, random.random()
    if not p["seen"] and (random.random() < .35 or g["stake"] > g["boot"] * 2):
        p["seen"] = True
    big = g["stake"] > g["boot"] * 6
    can_raise = g["stake"] * 2 <= g["boot"] * 16
    if not p["seen"]:
        if two and r < .15:
            show(g)
        elif r < .2 and can_raise:
            chaal(g, True)
        elif big and r < .5:
            pack(g)
        else:
            chaal(g, False)
    elif st == 0:
        if r < (.75 if g["stake"] > g["boot"] * 2 else .2):
            pack(g)
        else:
            chaal(g, False)
    elif st == 1:
        if (big and r < .6) or r < .05:
            pack(g)
        elif two and r < .35:
            show(g)
        else:
            chaal(g, False)
    elif two and r < .5:
        show(g)
    elif can_raise and r < .6:
        chaal(g, True)
    else:
        chaal(g, False)
    return True


def advance(g):
    return bot_move(g) or tick(g)


# ---------------- API ----------------
def view(g, code, pid):
    P = g["players"]
    me = find(g, pid)
    p = P[me]
    play = g["phase"] == "play"
    mine = play and not g["side"] and g["turn"] == me
    two = len(active(g)) == 2
    c1 = cost(g, p, False)
    prev = P[nxt(g, me, -1)] if play else p
    players = []
    for i, q in enumerate(P):
        reveal = (i == me and q["seen"]) or (g["phase"] == "over" and i in g["shown"])
        players.append(dict(
            name=q["name"], chips=q["chips"], bet=q["bet"], packed=q["packed"], seen=q["seen"],
            you=i == me, turn=play and i == g["turn"], win=g["phase"] == "over" and g["winner"] == i,
            left=q.get("left", False), wait=q.get("wait", False), bot=q.get("bot", False),
            cards=[cs(n) for n in q["hand"]] if reveal else None,
            rank=rank(q["hand"])[2] if reveal else None))
    side = None
    if g["side"]:
        side = f"{P[g['side'][0]]['name']} asks {P[g['side'][1]]['name']} for a side show"
    who = P[g["side"][1]] if g["side"] else P[g["turn"]]
    return dict(
        code=code, phase=g["phase"], pot=g["pot"], stake=g["stake"], boot=g["boot"], log=g["log"],
        result=g["result"], side=side, players=players, mode=g.get("mode", "classic"),
        coach=coach_tip(g, p), hand_no=g["hand_no"], chat=g.get("chat", [])[-30:], turn_name=who["name"] if play else "",
        turn_left=max(0, int(TURN_SECS - (time.time() - g.get("turn_at", 0)))) if play else 0,
        can=dict(
            chaal=mine, raise_=mine and g["stake"] * 2 <= g["boot"] * 16, pack=mine, show=mine and two,
            side=mine and not two and p["seen"] and prev["seen"],
            see=play and not p["seen"] and not p["packed"],
            answer=play and bool(g["side"]) and g["side"][1] == me,
            deal=pid == g["host"] and g["phase"] != "play" and sum(not x.get("left") for x in P) >= 2,
            close=pid == g["host"]),
        labels=dict(chaal=f"{'Chaal' if p['seen'] else 'Blind'} {c1}", raise_=f"Raise {cost(g, p, True)}",
                    show=f"Show {c1}", side=f"Side show {c1}",
                    deal="Next hand" if g["phase"] == "over" else "Deal"))


@app.post("/api/create")
async def create(req: Request):
    d = await req.json()
    boot = d.get("boot") if d.get("boot") in (10, 20, 50, 100) else 10
    code = "".join(random.choices(string.ascii_uppercase, k=4))
    g = new_game(d["pid"], boot, "muflis" if d.get("mode") == "muflis" else "classic")
    add(g, d["pid"], d.get("name", "Player"))
    for k in range(max(0, min(3, int(d.get("bots") or 0)))):
        add(g, f"bot{k}", BOTS[k])
        g["players"][-1]["bot"] = True
    save(code, g)
    return {"code": code}


@app.post("/api/join")
async def join(req: Request):
    d = await req.json()
    code = d.get("code", "").upper().strip()
    g = load(code)
    if not g:
        return err("No table with that code.", 404)
    if find(g, d["pid"]) is not None:
        return {"code": code}
    if not add(g, d["pid"], d.get("name", "Player")):
        return err("Table is full (6 players).")
    save(code, g)
    return {"code": code}


@app.get("/api/state")
async def state(code: str, pid: str):
    code = code.upper()
    g = load(code)
    if not g or find(g, pid) is None:
        return err("Table closed or not found.", 404)
    if advance(g):
        save(code, g)
    return view(g, code, pid)


@app.post("/api/act")
async def act(req: Request):
    d = await req.json()
    code = d.get("code", "").upper()
    g = load(code)
    if not g:
        return err("Table not found.", 404)
    P = g["players"]
    me = find(g, d["pid"])
    if me is None:
        return err("You are not at this table.", 403)
    advance(g)
    a, two = d.get("action"), len(active(g)) == 2
    if a == "deal":
        if d["pid"] != g["host"] or sum(not p.get("left") for p in P) < 2 or g["phase"] == "play":
            return err("Only the host can deal, with 2+ players.")
        deal(g)
    elif a == "see":
        if g["phase"] == "play" and not P[me]["packed"]:
            P[me]["seen"] = True
    elif a in ("yes", "no"):
        if g["phase"] != "play" or not g["side"] or g["side"][1] != me:
            return err("This isn't for you.")
        answer_side(g, a == "yes")
    elif a in ("chaal", "raise", "pack", "show", "side"):
        if g["phase"] != "play" or g["side"] or g["turn"] != me:
            return err("It isn't your turn.")
        if a == "chaal":
            chaal(g, False)
        elif a == "raise":
            if g["stake"] * 2 > g["boot"] * 16:
                return err("Maximum raise reached.")
            chaal(g, True)
        elif a == "pack":
            pack(g)
        elif a == "show":
            if not two:
                return err("Show needs exactly 2 players left.")
            show(g)
        else:
            if two or not (P[me]["seen"] and P[nxt(g, me, -1)]["seen"]):
                return err("Side show isn't allowed now.")
            ask_side(g)
    elif a == "chat":
        t = str(d.get("text", ""))[:120].strip()
        if t:
            g["chat"] = (g.get("chat", []) + [{"n": P[me]["name"], "t": t, "i": int(time.time() * 1000)}])[-30:]
    elif a == "leave":
        leave(g, me)
        if not any(not p.get("left") and not p.get("bot") for p in g["players"]):
            redis("DEL", "tp:" + code)
            return {"ok": True}
    elif a == "close":
        if d["pid"] != g["host"]:
            return err("Only the host can close the table.")
        redis("DEL", "tp:" + code)
        return {"ok": True}
    else:
        return err("Unknown action.")
    save(code, g)
    return {"ok": True}
