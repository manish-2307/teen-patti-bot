"""
Teen Patti Discord bot (Octro-style rules).

Setup:
  pip install "discord.py>=2.3" pillow
  set DISCORD_TOKEN=your_bot_token      (Windows)   or   export DISCORD_TOKEN=...   (Mac/Linux)
  python teen_patti_bot.py

Commands:  /teenpatti  /teenpatti_end  /teenpatti_rules
"""
import asyncio
import io
import math
import os
import random
import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

SUITS = "♠♥♦♣"
RL = {11: "J", 12: "Q", 13: "K", 14: "A"}
MAX_PLAYERS = 6
games = {}  # channel_id -> Game


# ---------------- hand ranking ----------------
def cs(n):
    r = 2 + n % 13
    return f"{RL.get(r, r)}{SUITS[n // 13]}"


def rank(h):
    """Trail > Pure sequence > Sequence > Color > Pair > High card. A-K-Q highest run, A-2-3 second."""
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


# ---------------- game state ----------------
class Player:
    def __init__(self, user, chips):
        self.user = user
        self.id = user.id
        self.name = user.display_name
        self.chips = chips
        self.bet = 0
        self.packed = False
        self.seen = False
        self.hand = []
        self.priv = None  # this player's private live table (ephemeral message)


class Game:
    def __init__(self, channel_id, host, boot):
        self.channel_id = channel_id
        self.host = host.id
        self.boot = boot
        self.players = []
        self.phase = "lobby"  # lobby | play | over
        self.hand_no = 0
        self.dealer = -1
        self.turn = 0
        self.stake = boot
        self.pot = 0
        self.side = None  # (from_idx, to_idx)
        self.side_msg = None
        self.log = []
        self.msg = None
        self.result = None
        self.shown = []
        self.winner = None
        self.won = 0

    def add(self, user):
        if any(p.id == user.id for p in self.players) or len(self.players) >= MAX_PLAYERS:
            return False
        self.players.append(Player(user, self.boot * 100))
        return True

    def active(self):
        return [p for p in self.players if not p.packed]

    def nxt(self, i, step=1):
        n, j = len(self.players), i
        while True:
            j = (j + step) % n
            if not self.players[j].packed or j == i:
                return j

    def cost(self, p, raise_):
        if p.seen:
            return self.stake * (4 if raise_ else 2)
        return self.stake * (2 if raise_ else 1)

    def pay(self, p, amount):
        amount = min(amount, p.chips)
        p.chips -= amount
        p.bet += amount
        self.pot += amount
        return amount

    def say(self, text):
        self.log = (self.log + [text])[-5:]

    def deal(self):
        deck = list(range(52))
        random.shuffle(deck)
        self.winner = None
        self.pot, self.stake, self.side, self.result, self.shown, self.log = 0, self.boot, None, None, [], []
        for p in self.players:
            if p.chips < self.boot:
                p.chips = self.boot * 100  # rebuy
            p.hand = [deck.pop() for _ in range(3)]
            p.bet, p.packed, p.seen = 0, False, False
            self.pay(p, self.boot)
        self.hand_no += 1
        self.dealer = (self.dealer + 1) % len(self.players)
        self.turn = (self.dealer + 1) % len(self.players)
        self.phase = "play"
        self.say(f"Hand {self.hand_no} dealt. Boot {self.boot} each.")

    def finish(self, w, text, shown=()):
        p = self.players[w]
        p.chips += self.pot
        self.result = f"{p.name} wins {self.pot} ({text})"
        self.winner, self.won = w, self.pot
        self.pot = 0
        self.shown = list(shown)
        self.phase = "over"
        self.side = None
        self.say(self.result)

    def only_left(self):
        return next(k for k, x in enumerate(self.players) if not x.packed)

    def chaal(self, raise_):
        p = self.players[self.turn]
        a = self.pay(p, self.cost(p, raise_))
        if raise_:
            self.stake *= 2
        self.say(f"{p.name} {'raised' if raise_ else 'played'} ({'seen' if p.seen else 'blind'}) {a}")
        self.turn = self.nxt(self.turn)

    def pack(self):
        p = self.players[self.turn]
        p.packed = True
        self.say(f"{p.name} packed.")
        if len(self.active()) == 1:
            self.finish(self.only_left(), "everyone else packed")
        else:
            self.turn = self.nxt(self.turn)

    def show(self):
        i = self.turn
        p = self.players[i]
        self.pay(p, self.cost(p, False))
        j = next(k for k, x in enumerate(self.players) if not x.packed and k != i)
        c = cmp(p.hand, self.players[j].hand)
        w, l = (i, j) if c > 0 else (j, i)  # tie: the player who asked for the show loses
        note = " (tie goes to the opponent)" if c == 0 else ""
        self.finish(w, f"{rank(self.players[w].hand)[2]} vs {rank(self.players[l].hand)[2]}{note}", [i, j])

    def ask_side(self):
        i = self.turn
        t = self.nxt(i, -1)
        self.pay(self.players[i], self.cost(self.players[i], False))
        self.side = (i, t)
        self.say(f"{self.players[i].name} asks {self.players[t].name} for a side show.")

    def answer_side(self, ok):
        f, t = self.side
        self.side = None
        a, b = self.players[f], self.players[t]
        if ok:
            loser = b if cmp(a.hand, b.hand) > 0 else a  # tie: requester packs
            loser.packed = True
            self.say(f"Side show: {loser.name} packs.")
            if len(self.active()) == 1:
                self.finish(self.only_left(), "side show", [f, t])
                return
        else:
            self.say(f"{b.name} declined the side show.")
        self.turn = self.nxt(f)


# ---------------- table pictures (Pillow) ----------------
GOLD = (240, 178, 79)
INK = (30, 30, 38)
RED = (190, 50, 45)
_fonts = {}


def font(size):
    size = max(8, int(size))
    if size in _fonts:
        return _fonts[size]
    f = None
    for nm in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            f = ImageFont.truetype(nm, size)
            break
        except OSError:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _fonts[size] = f
    return f


def clean(name, limit=10):
    t = "".join(c for c in name if c.isprintable() and ord(c) < 0x3000) or "Player"
    return t if len(t) <= limit else t[: limit - 1] + "…"


def draw_suit(d, s, cx, cy, u, col):
    if s == 2:  # diamond
        d.polygon([(cx, cy - u), (cx + u * .7, cy), (cx, cy + u), (cx - u * .7, cy)], fill=col)
    elif s == 1:  # heart
        d.ellipse([cx - u, cy - .9 * u, cx, cy + .1 * u], fill=col)
        d.ellipse([cx, cy - .9 * u, cx + u, cy + .1 * u], fill=col)
        d.polygon([(cx - .97 * u, cy - .15 * u), (cx + .97 * u, cy - .15 * u), (cx, cy + u)], fill=col)
    elif s == 0:  # spade
        d.polygon([(cx, cy - u), (cx - .97 * u, cy + .2 * u), (cx + .97 * u, cy + .2 * u)], fill=col)
        d.ellipse([cx - u, cy - .1 * u, cx, cy + .9 * u], fill=col)
        d.ellipse([cx, cy - .1 * u, cx + u, cy + .9 * u], fill=col)
        d.polygon([(cx, cy + .3 * u), (cx - .35 * u, cy + u), (cx + .35 * u, cy + u)], fill=col)
    else:  # club
        r = .45 * u
        for x, y in ((cx, cy - .5 * u), (cx - .55 * u, cy + .3 * u), (cx + .55 * u, cy + .3 * u)):
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        d.polygon([(cx, cy), (cx - .3 * u, cy + u), (cx + .3 * u, cy + u)], fill=col)


def card_img(n, w=90, h=128):
    """A playing card. n=None draws the face-down back."""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = max(3, w // 9)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=(251, 247, 238), outline=(20, 20, 25))
    if n is None:
        pad = max(3, w // 12)
        iw, ih = w - 2 * pad, h - 2 * pad
        inner = Image.new("RGB", (iw, ih), (107, 74, 143))
        idr = ImageDraw.Draw(inner)
        for k in range(-ih, iw, max(6, w // 8)):
            idr.line([(k, ih), (k + ih, 0)], fill=(88, 57, 122), width=max(2, w // 28))
        mask = Image.new("L", (iw, ih), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, iw - 1, ih - 1], radius=max(2, r - 1), fill=255)
        im.paste(inner, (pad, pad), mask)
        return im
    rk, s = 2 + n % 13, n // 13
    col = RED if s in (1, 2) else INK
    d.text((w * .1, h * .03), str(RL.get(rk, rk)), font=font(h * .27), fill=col)
    draw_suit(d, s, w * .24, h * .43, h * .06, col)
    draw_suit(d, s, w * .56, h * .7, h * .19, col)
    return im


def render_hand(hand):
    w, h, gap = 110, 156, 14
    im = Image.new("RGB", (len(hand) * (w + gap) + gap, h + 2 * gap), (52, 37, 71))
    for i, n in enumerate(hand):
        c = card_img(n, w, h)
        im.paste(c, (gap + i * (w + gap), gap), c)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return buf


def render_table(g, viewer=None):
    """Public table (viewer=None) or a private view with the viewer's own cards face up."""
    W, H = 960, 620
    me = g.players[viewer] if viewer is not None and 0 <= viewer < len(g.players) else None
    extra = 190 if me and me.seen else 0
    im = Image.new("RGB", (W, H + extra), (23, 17, 32))
    d = ImageDraw.Draw(im)
    d.ellipse([30, 40, W - 30, H - 40], fill=(92, 64, 120))
    d.ellipse([52, 62, W - 52, H - 62], fill=(52, 37, 71))
    cx, cy = W // 2, H // 2
    if g.phase == "over" and g.winner is not None:
        d.text((cx, cy - 45), "WINNER", font=font(26), fill=GOLD, anchor="mm")
        d.text((cx, cy), clean(g.players[g.winner].name, 14), font=font(46), fill=(255, 255, 255), anchor="mm")
        d.text((cx, cy + 50), f"+{g.won}", font=font(34), fill=GOLD, anchor="mm")
    else:
        d.text((cx, cy - 50), "POT", font=font(24), fill=(200, 190, 215), anchor="mm")
        d.text((cx, cy), str(g.pot), font=font(64), fill=GOLD, anchor="mm")
        d.text((cx, cy + 50), f"Stake {g.stake}  |  Hand {g.hand_no}", font=font(22), fill=(200, 190, 215), anchor="mm")
    n = len(g.players)
    for i, p in enumerate(g.players):
        ang = math.pi / 2 + 2 * math.pi * i / n
        sx = cx + int(math.cos(ang) * (W / 2 - 150))
        sy = cy + int(math.sin(ang) * (H / 2 - 85))
        left, top = sx - 115, sy - 52
        turn = g.phase == "play" and i == g.turn
        won = g.phase == "over" and i == g.winner
        outline = GOLD if turn else ((110, 220, 150) if won else (120, 100, 140))
        d.rounded_rectangle([left, top, left + 230, top + 104], radius=16,
                            fill=(40, 30, 50) if p.packed else (30, 22, 42),
                            outline=outline, width=5 if (turn or won) else 2)
        dim = (140, 130, 150)
        d.text((left + 12, top + 8), clean(p.name), font=font(20), fill=dim if p.packed else (255, 255, 255))
        d.text((left + 12, top + 34), str(p.chips), font=font(26), fill=dim if p.packed else GOLD)
        status = "Packed" if p.packed else ("Seen" if p.seen else "Blind")
        d.text((left + 12, top + 72), f"{status} | in {p.bet}", font=font(14), fill=(200, 190, 215))
        if i == g.dealer and g.phase != "lobby":
            d.ellipse([left + 206, top - 10, left + 236, top + 20], fill=(255, 255, 255))
            d.text((left + 221, top + 5), "D", font=font(18), fill=INK, anchor="mm")
        if not p.packed:
            face = (g.phase == "over" and i in g.shown) or (i == viewer and p.seen)
            for k, c in enumerate(p.hand):
                ci = card_img(c if face else None, 30, 43)
                im.paste(ci, (left + 125 + k * 33, top + 30), ci)
    if extra:
        d.text((W // 2, H + 8), f"Your hand: {rank(me.hand)[2]}", font=font(24), fill=GOLD, anchor="mt")
        cw, ch, gap = 90, 128, 16
        x0 = (W - (3 * cw + 2 * gap)) // 2
        for k, c in enumerate(me.hand):
            ci = card_img(c, cw, ch)
            im.paste(ci, (x0 + k * (cw + gap), H + 46), ci)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return buf


# ---------------- embeds ----------------
def embed(g):
    e = discord.Embed(title=f"Teen Patti: hand {g.hand_no}" if g.hand_no else "Teen Patti table",
                      color=0xB5651D)
    if g.phase == "lobby":
        e.description = f"Boot **{g.boot}** · each player starts with **{g.boot * 100}** chips. Up to {MAX_PLAYERS} players."
    elif g.phase == "play":
        e.description = f"**Pot:** {g.pot}   **Stake:** {g.stake}"
    else:
        e.description = g.result or ""
    lines = []
    for i, p in enumerate(g.players):
        mark = "👉 " if g.phase == "play" and i == g.turn else ""
        status = "packed" if p.packed else ("seen" if p.seen else "blind")
        line = f"{mark}**{p.name}**: {p.chips} chips"
        if g.phase != "lobby":
            line += f" · {status} · in {p.bet}"
        if g.phase == "over" and i in g.shown:
            line += f"\n    {' '.join(cs(n) for n in p.hand)} ({rank(p.hand)[2]})"
        lines.append(line)
    if g.phase == "lobby":
        e.add_field(name="Players", value="\n".join(lines) or "No one yet", inline=False)
    if g.log and g.phase != "lobby":
        e.add_field(name="Log", value="\n".join(g.log), inline=False)
    return e


async def refresh(g):
    view = {"lobby": LobbyView, "play": TableView, "over": EndView}[g.phase](g)
    emb = embed(g)
    if g.phase == "lobby":
        return await g.msg.edit(embed=emb, view=view)
    buf = await asyncio.to_thread(render_table, g)
    emb.set_image(url="attachment://table.png")
    await g.msg.edit(embed=emb, view=view, attachments=[discord.File(buf, "table.png")])
    await asyncio.gather(*(update_private(g, i, p) for i, p in enumerate(g.players) if p.priv))


async def update_private(g, i, p):
    try:
        buf = await asyncio.to_thread(render_table, g, i)
        await p.priv.edit(attachments=[discord.File(buf, "table.png")])
    except Exception:
        p.priv = None  # expired (Discord allows ~15 minutes); press See cards again


# ---------------- views ----------------
class LobbyView(discord.ui.View):
    def __init__(self, g):
        super().__init__(timeout=None)
        self.g = g

    @discord.ui.button(label="Join", style=discord.ButtonStyle.primary)
    async def join(self, interaction, button):
        await interaction.response.defer()
        self.g.add(interaction.user)
        await refresh(self.g)

    @discord.ui.button(label="Deal (host)", style=discord.ButtonStyle.success)
    async def start(self, interaction, button):
        g = self.g
        if interaction.user.id != g.host:
            return await interaction.response.send_message("Only the host can deal.", ephemeral=True)
        if len(g.players) < 2:
            return await interaction.response.send_message("Need at least 2 players.", ephemeral=True)
        await interaction.response.defer()
        g.deal()
        await refresh(g)


class EndView(discord.ui.View):
    def __init__(self, g):
        super().__init__(timeout=None)
        self.g = g

    @discord.ui.button(label="Next hand (host)", style=discord.ButtonStyle.success)
    async def nxt(self, interaction, button):
        if interaction.user.id != self.g.host:
            return await interaction.response.send_message("Only the host can deal.", ephemeral=True)
        await interaction.response.defer()
        self.g.deal()
        await refresh(self.g)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.primary)
    async def join(self, interaction, button):
        await interaction.response.defer()
        self.g.add(interaction.user)
        await refresh(self.g)

    @discord.ui.button(label="Close table (host)", style=discord.ButtonStyle.danger)
    async def close(self, interaction, button):
        if interaction.user.id != self.g.host:
            return await interaction.response.send_message("Only the host can close the table.", ephemeral=True)
        await interaction.response.defer()
        games.pop(self.g.channel_id, None)
        await self.g.msg.edit(content="Table closed.", view=None)


class TableView(discord.ui.View):
    def __init__(self, g):
        super().__init__(timeout=None)
        self.g = g
        p = g.players[g.turn]
        two = len(g.active()) == 2
        c1 = g.cost(p, False)
        prev = g.players[g.nxt(g.turn, -1)]
        self.chaal_btn.label = f"{'Chaal' if p.seen else 'Blind'} {c1}"
        self.raise_btn.label = f"Raise {g.cost(p, True)}"
        self.raise_btn.disabled = g.stake * 2 > g.boot * 16
        self.show_btn.label = f"Show {c1}"
        self.show_btn.disabled = not two
        self.side_btn.label = f"Side show {c1}"
        self.side_btn.disabled = two or not (p.seen and prev.seen)
        if g.side:
            for b in (self.chaal_btn, self.raise_btn, self.show_btn, self.side_btn, self.pack_btn):
                b.disabled = True

    async def guard(self, interaction):
        g = self.g
        if g.phase != "play" or g.side or interaction.user.id != g.players[g.turn].id:
            await interaction.response.send_message("It isn’t your turn.", ephemeral=True)
            return False
        await interaction.response.defer()
        return True

    @discord.ui.button(label="Chaal", style=discord.ButtonStyle.primary, row=0)
    async def chaal_btn(self, interaction, button):
        if await self.guard(interaction):
            self.g.chaal(False)
            await refresh(self.g)

    @discord.ui.button(label="Raise", style=discord.ButtonStyle.secondary, row=0)
    async def raise_btn(self, interaction, button):
        if await self.guard(interaction):
            self.g.chaal(True)
            await refresh(self.g)

    @discord.ui.button(label="Pack", style=discord.ButtonStyle.danger, row=0)
    async def pack_btn(self, interaction, button):
        if await self.guard(interaction):
            self.g.pack()
            await refresh(self.g)

    @discord.ui.button(label="Show", style=discord.ButtonStyle.secondary, row=1)
    async def show_btn(self, interaction, button):
        if await self.guard(interaction):
            self.g.show()
            await refresh(self.g)

    @discord.ui.button(label="Side show", style=discord.ButtonStyle.secondary, row=1)
    async def side_btn(self, interaction, button):
        g = self.g
        if await self.guard(interaction):
            g.ask_side()
            await refresh(g)
            target = g.players[g.side[1]]
            g.side_msg = await interaction.channel.send(
                f"<@{target.id}>, **{g.players[g.side[0]].name}** asks you for a side show. Accept?",
                view=SideView(g))

    @discord.ui.button(label="See cards", style=discord.ButtonStyle.success, row=2)
    async def see_btn(self, interaction, button):
        g = self.g
        p = next((x for x in g.players if x.id == interaction.user.id), None)
        if not p:
            return await interaction.response.send_message("You’re not in this hand.", ephemeral=True)
        idx = g.players.index(p)
        newly = not p.seen
        p.seen = True

        async def img():
            return discord.File(await asyncio.to_thread(render_table, g, idx), "table.png")

        acked = False
        if p.priv:  # update the private table you already have
            acked = True
            await interaction.response.defer()
            try:
                await p.priv.edit(attachments=[await img()])
                if newly:
                    await refresh(g)
                return
            except discord.HTTPException:
                p.priv = None
        if acked:
            p.priv = await interaction.followup.send(file=await img(), ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(file=await img(), ephemeral=True)
            p.priv = await interaction.original_response()
        if newly:
            await refresh(g)


class SideView(discord.ui.View):
    def __init__(self, g):
        super().__init__(timeout=None)
        self.g = g

    async def answer(self, interaction, ok):
        g = self.g
        if not g.side or interaction.user.id != g.players[g.side[1]].id:
            return await interaction.response.send_message("This isn’t for you.", ephemeral=True)
        await interaction.response.defer()
        g.answer_side(ok)
        await interaction.message.delete()
        await refresh(g)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction, button):
        await self.answer(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction, button):
        await self.answer(interaction, False)


# ---------------- bot + commands ----------------
client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    for guild in client.guilds:  # per-server sync shows commands instantly
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        print(f"Synced {len(synced)} commands to {guild.name}")
    print(f"Logged in as {client.user}")


@tree.command(name="teenpatti", description="Start a Teen Patti table in this channel")
@app_commands.describe(boot="Boot amount (everyone starts with 100x this)")
@app_commands.choices(boot=[app_commands.Choice(name=str(b), value=b) for b in (10, 20, 50, 100)])
async def teenpatti(interaction: discord.Interaction, boot: app_commands.Choice[int] = None):
    if interaction.channel_id in games:
        return await interaction.response.send_message("A table is already open in this channel.", ephemeral=True)
    g = Game(interaction.channel_id, interaction.user, boot.value if boot else 10)
    g.add(interaction.user)
    games[interaction.channel_id] = g
    await interaction.response.send_message("Table created. Others can press Join.", ephemeral=True)
    g.msg = await interaction.channel.send(embed=embed(g), view=LobbyView(g))


@tree.command(name="teenpatti_end", description="Close the Teen Patti table in this channel (host only)")
async def teenpatti_end(interaction: discord.Interaction):
    g = games.get(interaction.channel_id)
    if not g or interaction.user.id != g.host:
        return await interaction.response.send_message("No table here, or you aren’t the host.", ephemeral=True)
    games.pop(interaction.channel_id)
    await g.msg.edit(content="Table closed.", view=None)
    await interaction.response.send_message("Closed.", ephemeral=True)


@tree.command(name="teenpatti_rules", description="Show the Teen Patti rules")
async def rules(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Teen Patti**\n"
        "Everyone puts in the boot. Play blind (pay the stake) or look at your cards (pay double).\n"
        "**Raise** doubles the stake (max 16x boot). **Pack** to fold.\n"
        "**Show** (2 players left): pay to compare hands; a tie goes to the opponent.\n"
        "**Side show**: a seen player asks the previous seen player to compare; the lower hand packs, a tie packs the asker.\n"
        "**Ranking:** Trail > Pure sequence > Sequence > Color > Pair > High card. A-K-Q is the top run, A-2-3 is second.",
        ephemeral=True)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    client.run(token)
