"""
⚽🕶️ ФУТБОЛЬНАЯ МАФИЯ — мультиплеерный сервер

Игра для компании друзей: каждый игрок тайно получает роль знаменитого
футболиста (имя + фото + подсказка из Википедии), а часть игроков — мафия,
которая знает «своих» и каждую ночь «убивает» легенду футбола.
Горожане должны вычислить мафию по подсказкам и голосованием.

Стек: FastAPI + WebSockets (реальный-time мультиплеер) + MediaWiki API (ru/en).

Запуск:  python server.py   →  http://localhost:8000
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("football-mafia")

WIKI_RU = "https://ru.wikipedia.org/w/api.php"
WIKI_EN = "https://en.wikipedia.org/w/api.php"
API_HEADERS = {
    "User-Agent": "FootballMafiaPartyGame/1.0 (https://localhost; party game for friends) python-httpx",
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Википедия
# ---------------------------------------------------------------------------

_cache: dict[str, object] = {}


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    if key in _cache:
        return _cache[key]  # type: ignore[return-value]
    data: dict = {}
    for attempt in range(3):
        try:
            r = await client.get(url, params=params, timeout=15)
            if r.status_code == 403 and attempt < 2:   # wiki иногда режет запросы — retry
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:  # noqa: BLE001
            log.warning("wiki request failed (%s): %s", url, e)
            data = {}
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
    _cache[key] = data
    return data


async def wiki_find_title(client: httpx.AsyncClient, name: str) -> Optional[str]:
    """Найти название статьи в ру-вики по имени футболиста."""
    data = await _get(client, WIKI_RU, {
        "action": "query", "list": "search", "srsearch": f"{name} футболист",
        "srlimit": 6, "srprop": "", "format": "json",
    })
    results = data.get("query", {}).get("search", [])
    tokens = [re.sub(r"[^\w]", "", w.lower()) for w in re.split(r"[\s,\-']+", name)]
    tokens = [t for t in tokens if len(t) >= 4]
    for item in results:
        tnorm = re.sub(r"[^\w]", "", item["title"].lower())
        if tokens and any(t in tnorm for t in tokens[:2]):
            return item["title"]
    if results:
        return results[0]["title"]
    # fallback: поиск в en-wiki
    data = await _get(client, WIKI_EN, {
        "action": "query", "list": "search", "srsearch": f'"{name}" footballer',
        "srlimit": 3, "srprop": "", "format": "json",
    })
    results = data.get("query", {}).get("search", [])
    return results[0]["title"] if results else None


async def wiki_extract(client: httpx.AsyncClient, title: str) -> str:
    """Первый абзац статьи: сначала ру-вики, потом en-вики."""
    for url in (WIKI_RU, WIKI_EN):
        data = await _get(client, url, {
            "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
            "redirects": 1, "titles": title, "format": "json",
        })
        for p in data.get("query", {}).get("pages", {}).values():
            ex = (p.get("extract") or "").strip()
            if ex:
                return ex
    return ""


async def wiki_image(client: httpx.AsyncClient, title: str) -> Optional[str]:
    """Портретная миниатюра статьи."""
    for url in (WIKI_RU, WIKI_EN):
        data = await _get(client, url, {
            "action": "query", "prop": "pageimages", "piprop": "thumbnail",
            "pithumbsize": 320, "redirects": 1, "titles": title, "format": "json",
        })
        for p in data.get("query", {}).get("pages", {}).values():
            src = p.get("thumbnail", {}).get("source")
            if src:
                return src
    return None


def make_hint(extract: str, footballer_display: str, max_len: int = 160) -> str:
    """Сжать первый абзац в подсказку и убрать из неё имя героя,
    чтобы подсказка не выдавала футболиста сразу."""
    text = extract.split("\n\n")[0]
    text = re.sub(r"\s+", " ", text).strip()
    # вырезаем имя/фамилию игрока из текста подсказки
    parts = set()
    for w in re.split(r"[^\w\-']+", footballer_display.lower()):
        if len(w) > 3:
            parts.add(w)
    if parts:
        pattern = "|".join(re.escape(p) for p in sorted(parts, key=len, reverse=True))
        text = re.sub(pattern, "•", text, flags=re.IGNORECASE)
    text = re.sub(r"[•\s]*(?:•[ ,]*)+", " • ", text)  # склеить повторы точек
    text = re.sub(r"\(.*?\)", "", text)               # скобки с датами — лишнее
    text = re.sub(r"\s+", " ", text).strip(" •,-—:")
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


async def fetch_footballer(client: httpx.AsyncClient, pool_entry: str) -> dict:
    """Собрать карточку футболиста из Википедии."""
    title = await wiki_find_title(client, pool_entry) or pool_entry
    display = title.split(",")[0].strip()
    extract, image = await asyncio.gather(wiki_extract(client, title), wiki_image(client, title))
    hint = make_hint(extract, display) if extract else \
        "Легенда футбола, о которой нет статьи — играйте вслепую!"
    return {"title": title, "display": display, "hint": hint, "image": image}


# ---------------------------------------------------------------------------
# Пул футболистов (уточняется через поиск Википедии при раздаче ролей)
# ---------------------------------------------------------------------------

FOOTBALLERS_POOL = [
    "Лионель Месси", "Криштиану Роналду", "Диего Марадона", "Пеле", "Зинедин Зидан",
    "Роналдо", "Рональдиньо", "Тьерри Анри", "Франц Беккенбауэр", "Лотар Маттеус",
    "Мишель Платини", "Йохан Кройф", "Марко ван Бастен", "Рууд Гуллит", "Алан Ширер",
    "Паоло Мальдини", "Алессандро Дель Пьеро", "Франческо Тотти", "Джанлуиджи Буффон",
    "Фабио Каннаваро", "Андрей Шевченко", "Лев Яшин", "Олег Блохин", "Игорь Численко",
    "Александр Мостовой", "Андрей Аршавин", "Денис Черышев", "Сергей Ребров",
    "Килиан Мбаппе", "Неймар", "Луис Суарес", "Эдинсон Кавани", "Серхио Агуэро",
    "Хави", "Андрес Иньеста", "Икер Касильяс", "Карлес Пуйоль", "Жерар Пике",
    "Серхио Рамос", "Лука Модрич", "Иван Ракитич", "Роберт Левандовски",
    "Пьер-Эмерик Обамеянг", "Кевин Де Брюне", "Эден Азар", "Тибо Куртуа",
    "Виргил ван Дейк", "Мохамед Салах", "Садио Мане", "Рияд Махрез", "Гарри Кейн",
    "Уэйн Руни", "Фрэнк Лэмпард", "Стивен Джеррард", "Джон Терри", "Петр Чех",
    "Дидье Дрогба", "Кака", "Роберто Карлос", "Кафу", "Ривелино", "Гарринча",
    "Джанлука Виалли", "Роберто Баджо", "Алессандро Неста", "Клаудио Маркизио",
    "Антуан Гризманн", "Поль Погба", "Нголо Канте", "Оливье Жиро",
    "Уго Льорис", "Мануэль Нойер", "Мирослав Клозе", "Томас Мюллер",
    "Бастиан Швайнштайгер", "Месут Озил", "Марко Ройс", "Лерой Сане", "Сердж Гнабри",
    "Флориан Виртц", "Джуд Беллингем", "Винисиус Жуниор", "Родриго",
    "Федерико Вальверде", "Тони Кроос", "Эрлинг Холанд", "Мартин Эдегор",
    "Виктор Осимхен", "Хвича Кварацхелия", "Алексис Мак-Аллистер", "Энцо Фернандес",
    "Хулиан Альварес", "Лаутаро Мартинес", "Бруну Фернандеш", "Бернарду Сильва",
    "Жоау Феликс", "Рубен Диаш", "Дيوго Жота", "Маркиньос", "Алиссон Бекер",
    "Эдер Милитао", "Ричарлисон", "Габриэл Жезус", "Сон Хын Мин", "Масон Маунт",
    "Джек Грилиш", "Букайо Сака", "Деклан Райс", "Кай Хавертц", "Тимо Вернер",
    "Ашраф Хакими", "Усман Дембеле", "Ферран Торрес", "Дани Ольмо", "Педри",
    "Ансу Фати", "Ламин Ямаль", "Йошко Гвардиол", "Душан Влахович",
    "Александар Митрович", "Георгий Мамардашвили", "Андре Онана", "Ян Облак",
    "Ян Зоммер", "Гарри Магуайр", "Люк Шоу", "Джон Стоунз", "Кайл Уокер",
    "Джордан Хендерсон", "Иван Тони", "Доминик Соланке", "Коул Палмер",
    "Джеррод Боуэн", "Давид де Хеа", "Уго Бардей", "Маркус Рашфорд",
    "Джейдон Санчо", "Фил Фоуден", "Родри", "Кристиан Пулишич",
    "Уэстон Маккенни", "Альфонсо Дэвис", "Джонатан Та", "Жером Боатенг",
    "Давид Алаба", "Марсель Забитцер", "Юрген Клинсманн", "Оливер Кан",
    "Андон Субисарета", "Фернандо Йерро", "Рауль Гонсалес", "Фернандо Торрес",
    "Давид Вилья", "Сантьяго Канисарес", "Виктор Вальдес", "Хуан Мата",
    "Сами Хююпя", "Фернандо Редондо", "Клаудио Лопес", "Эмилиано Мартинес",
    "Лотар Голуб", "Гжегож Лято", "Боннино Оскар", "Омар Сивори",
]
FOOTBALLERS_POOL = list(dict.fromkeys(FOOTBALLERS_POOL))  # убрать дубликаты

# ---------------------------------------------------------------------------
# Модель игры
# ---------------------------------------------------------------------------

NIGHT_SECONDS = 40
DAY_SECONDS = 150
VOTE_SECONDS = 40
REVEAL_SECONDS = 10
MIN_PLAYERS = 4
MAX_PLAYERS = 12


@dataclass
class Player:
    sid: str
    ws: WebSocket
    name: str = ""
    alive: bool = True
    is_mafia: bool = False
    footballer: str = ""
    hint: str = ""
    image: Optional[str] = None
    vote_for: Optional[str] = None
    ready: bool = False

    def public(self, room: "Room") -> dict:
        return {
            "sid": self.sid, "name": self.name, "alive": self.alive,
            "isHost": self.sid == room.host_sid, "ready": self.ready,
            "voted": self.vote_for is not None,
            "revealedRole": (not self.alive) or room.state == "gameover",
            "wasMafia": self.is_mafia,
            "footballer": self.footballer if ((not self.alive) or room.state == "gameover") else None,
        }

    def private(self, room: "Room") -> dict:
        mates = [p.name for p in room.players.values() if p.is_mafia and p.sid != self.sid]
        return {
            "sid": self.sid, "name": self.name, "alive": self.alive,
            "isMafia": self.is_mafia, "footballer": self.footballer, "hint": self.hint,
            "image": self.image, "mates": mates if self.is_mafia else [],
            "voteFor": room.players[self.vote_for].name if self.vote_for and self.vote_for in room.players else None,
            "nightTarget": (room.players[room.night_target].name
                            if room.night_target and room.night_target in room.players else None)
            if self.is_mafia else None,
        }


@dataclass
class Room:
    code: str
    host_sid: str = ""
    players: dict[str, Player] = field(default_factory=dict)
    state: str = "lobby"     # lobby | dealing | night | day | voting | reveal | gameover
    round: int = 0
    timer_ends_at: float = 0.0
    messages: list[dict] = field(default_factory=list)
    mafia_chat: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    last_killed: Optional[str] = None
    last_voted_out: Optional[str] = None
    last_voted_was_mafia: Optional[bool] = None
    night_target: Optional[str] = None
    task: Optional[asyncio.Task] = None
    _winner_cache: Optional[str] = None

    def living(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def living_mafia(self) -> list[Player]:
        return [p for p in self.living() if p.is_mafia]

    def winner(self) -> Optional[str]:
        if self.state != "gameover":
            return None
        return self._winner_cache

    def public(self) -> dict:
        return {
            "code": self.code, "state": self.state, "round": self.round,
            "hostSid": self.host_sid, "timerEndsAt": self.timer_ends_at,
            "serverTime": time.time(),
            "players": [p.public(self) for p in self.players.values()],
            "messages": self.messages[-100:], "log": self.log[-30:],
            "lastKilled": self.last_killed, "lastVotedOut": self.last_voted_out,
            "lastVotedWasMafia": self.last_voted_was_mafia,
            "winner": self.winner(),
        }


ROOMS: dict[str, Room] = {}
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_code() -> str:
    while True:
        c = "".join(random.choices(CODE_CHARS, k=5))
        if c not in ROOMS:
            return c


def gen_nick() -> str:
    adj = ["Быстрый", "Хитрый", "Легендарный", "Неудержимый", "Крепкий",
           "Летучий", "Дерзкий", "Ветреный", "Стальной", "Юрский"]
    noun = ["Форвард", "Хавбек", "Голкипер", "Либеро", "Бомбардир",
            "Легионер", "Капитан", "Инсайд", "Джокер", "Плеймейкер"]
    return f"{random.choice(adj)}-{random.choice(noun)}"


# ---------------------------------------------------------------------------
# Отправка состояния
# ---------------------------------------------------------------------------

async def send(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


async def broadcast(room: Room, payload: dict):
    for p in list(room.players.values()):
        await send(p.ws, payload)


async def sync_all(room: Room):
    pub = room.public()
    for p in list(room.players.values()):
        await send(p.ws, {
            "type": "sync", "room": pub, "you": p.private(room),
            "mafiaChat": room.mafia_chat[-80:] if p.is_mafia else None,
        })


def push_msg(room: Room, sender: str, text: str, channel: str = "public"):
    entry = {"id": uuid.uuid4().hex[:8], "sender": sender, "text": text,
             "ts": time.time(), "channel": channel}
    (room.mafia_chat if channel == "mafia" else room.messages).append(entry)
    return entry


def push_log(room: Room, text: str):
    room.log.append({"id": uuid.uuid4().hex[:8], "text": text, "ts": time.time()})


# ---------------------------------------------------------------------------
# Фазы игры
# ---------------------------------------------------------------------------

def cancel_timer(room: Room):
    if room.task and not room.task.done():
        room.task.cancel()
    room.task = None


def arm_timer(room: Room, seconds: float, on_end):
    cancel_timer(room)
    room.timer_ends_at = time.time() + seconds

    async def runner():
        try:
            end = room.timer_ends_at
            while time.time() < end:
                await asyncio.sleep(min(0.5, end - time.time()))
            if room.timer_ends_at != end:
                return
            await on_end(room)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("phase timer crashed")

    room.task = asyncio.create_task(runner())


async def phase_night(room: Room):
    room.state = "night"
    room.round += 1
    room.night_target = None
    for p in room.players.values():
        p.vote_for = None
    push_log(room, f"🌙 Раунд {room.round}. Город засыпает… Мафия открывает глаза.")
    await sync_all(room)
    arm_timer(room, NIGHT_SECONDS, finish_night)


async def finish_night(room: Room):
    killed = None
    victims = [p for p in room.living() if not p.is_mafia]
    if victims:
        t = room.night_target
        victim = room.players[t] if t and t in room.players and room.players[t].alive \
            else random.choice(victims)
        victim.alive = False
        killed = victim.name
        push_log(room, f"🔪 Ночью мафия «трансфернула» игрока: **{killed}**")
    room.last_killed = killed
    room.night_target = None
    await phase_day(room, killed)


async def phase_day(room: Room, killed: Optional[str]):
    if killed:
        push_log(room, f"☀️ Утро. Горожанин **{killed}** найден мёртвым у стадиона!")
    else:
        push_log(room, "☀️ Утро. Ночь была тихой — никто не пострадал.")
    await sync_all(room)
    if check_win(room):
        return
    room.state = "day"
    await sync_all(room)
    arm_timer(room, DAY_SECONDS, lambda r: phase_voting(r))


async def phase_voting(room: Room):
    room.state = "voting"
    for p in room.players.values():
        p.vote_for = None
    push_log(room, "🗳️ Экстренное собрание! Голосуем за подозреваемого.")
    await sync_all(room)
    arm_timer(room, VOTE_SECONDS, resolve_vote)


async def resolve_vote(room: Room):
    counts: dict[str, int] = {}
    for p in room.living():
        if p.vote_for and p.vote_for in room.players and room.players[p.vote_for].alive:
            counts[p.vote_for] = counts.get(p.vote_for, 0) + 1
    out_name, was_mafia = None, None
    if counts:
        mx = max(counts.values())
        tops = [sid for sid, c in counts.items() if c == mx]
        if len(tops) == 1:
            victim = room.players[tops[0]]
            victim.alive = False
            out_name, was_mafia = victim.name, victim.is_mafia
            if was_mafia:
                push_log(room, f"🔨 Горожане изгнали **{out_name}** — и это была МАФИЯ! 🎉")
            else:
                push_log(room, f"💀 Горожане изгнали **{out_name}**. Он был мирным жителем…")
        else:
            push_log(room, "⚖️ Голоса разделились — никто не изгнан.")
    else:
        push_log(room, "🤷 Никто не проголосовал.")
    room.last_voted_out, room.last_voted_was_mafia = out_name, was_mafia
    await sync_all(room)
    if check_win(room):
        return
    room.state = "reveal"
    await sync_all(room)
    arm_timer(room, REVEAL_SECONDS, lambda r: phase_night(r))


def check_win(room: Room) -> bool:
    m, c = len(room.living_mafia()), len([p for p in room.living() if not p.is_mafia])
    w = None
    if m == 0:
        w = "citizens"
    elif m >= c:
        w = "mafia"
    if not w:
        return False
    cancel_timer(room)
    room.state = "gameover"
    room._winner_cache = w
    room.timer_ends_at = 0
    push_log(room, "🏆 ПОБЕДА ГОРОЖАН! Вся футбольная мафия разоблачена."
             if w == "citizens" else "🕶️ МАФИЯ ПОБЕДИЛА! Город захвачен.")
    return True


async def deal_roles(room: Room, http: httpx.AsyncClient):
    """Раздать роли: тянем футболистов с Википедии параллельно."""
    room.state = "dealing"
    room.timer_ends_at = 0
    push_log(room, "📰 Википедия подгружает досье легенд футбола…")
    await sync_all(room)

    n = len(room.players)
    pool = random.sample(FOOTBALLERS_POOL, min(len(FOOTBALLERS_POOL), n * 2))
    cards = await asyncio.gather(*[fetch_footballer(http, name) for name in pool])
    good = [c for c in cards if c["hint"]]
    random.shuffle(good)

    roles = [True] * mafia_count(n) + [False] * (n - mafia_count(n))
    random.shuffle(roles)
    used: set[str] = set()
    plist = list(room.players.values())
    for i, p in enumerate(plist):
        card = next((c for c in good if c["title"] not in used), good[i % len(good)])
        used.add(card["title"])
        p.is_mafia = roles[i]
        p.footballer = card["display"]
        p.hint = card["hint"]
        p.image = card["image"]
        p.alive = True
        p.ready = False
        p.vote_for = None
    room.round = 0
    room.messages, room.mafia_chat, room.log = [], [], []
    room.last_killed = room.last_voted_out = None
    room.last_voted_was_mafia = None
    push_log(room, "🎬 Роли розданы! Вы — тайная легенда футбола. Но кто-то из вас играет за мафию…")
    await sync_all(room)
    await asyncio.sleep(4)          # время посмотреть свою карту
    await phase_night(room)


def mafia_count(n: int) -> int:
    return 1 if n <= 6 else (2 if n <= 9 else 3)


# ---------------------------------------------------------------------------
# Сервер
# ---------------------------------------------------------------------------

app = FastAPI(title="Футбольная мафия")
http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(headers=API_HEADERS)


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()


@app.get("/api/ping")
async def ping():
    return {"ok": True, "rooms": len(ROOMS)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sid = uuid.uuid4().hex[:10]
    try:
        hello = json.loads(await ws.receive_text())
    except Exception:  # noqa: BLE001
        await ws.close()
        return

    op = hello.get("op")
    name = (hello.get("name") or "").strip()[:20] or gen_nick()
    room: Optional[Room] = None

    if op == "create":
        room = Room(code=gen_code(), host_sid=sid)
        ROOMS[room.code] = room
    elif op == "join":
        code = (hello.get("code") or "").strip().upper()
        room = ROOMS.get(code)
        if not room:
            await send(ws, {"type": "fatal", "message": "Комната не найдена. Проверьте код."})
            await ws.close()
            return
        if room.state not in ("lobby",):
            await send(ws, {"type": "fatal", "message": "Игра уже началась — присоединиться нельзя."})
            await ws.close()
            return
        if len(room.players) >= MAX_PLAYERS:
            await send(ws, {"type": "fatal", "message": f"Комната полная (макс. {MAX_PLAYERS})."})
            await ws.close()
            return
        if any(p.name.lower() == name.lower() for p in room.players.values()):
            name = f"{name}-{random.randint(10, 99)}"
    else:
        await send(ws, {"type": "fatal", "message": "Неизвестная команда."})
        await ws.close()
        return

    assert room is not None and http_client is not None
    player = Player(sid=sid, ws=ws, name=name)
    room.players[sid] = player
    push_msg(room, "система", f"👋 **{name}** присоединился к комнате")
    await sync_all(room)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mt = msg.get("type")

            if mt == "chat":
                text = (msg.get("text") or "").strip()[:500]
                if not text:
                    continue
                ch = "public"
                if player.is_mafia and msg.get("channel") == "mafia" and room.state in ("night", "day", "voting"):
                    ch = "mafia"
                entry = push_msg(room, player.name, text, ch)
                if ch == "mafia":
                    for mp in room.players.values():
                        if mp.is_mafia:
                            await send(mp.ws, {"type": "mafia_msg", "msg": entry})
                else:
                    await broadcast(room, {"type": "chat", "msg": entry})

            elif mt == "kill":
                t = msg.get("target")
                if room.state == "night" and player.is_mafia and t in room.players \
                        and room.players[t].alive and not room.players[t].is_mafia:
                    room.night_target = t
                    push_log(room, f"🕶️ Мафия нацелилась на **{room.players[t].name}**")
                    await sync_all(room)

            elif mt == "vote":
                t = msg.get("target")
                if room.state == "voting" and player.alive:
                    if t == "clear":
                        player.vote_for = None
                    elif t in room.players and room.players[t].alive and t != sid:
                        player.vote_for = t
                    await sync_all(room)

            elif mt == "start":
                if sid == room.host_sid and room.state == "lobby" and len(room.players) >= MIN_PLAYERS:
                    asyncio.create_task(deal_roles(room, http_client))

            elif mt == "skip":
                if sid != room.host_sid:
                    continue
                if room.state == "night":
                    cancel_timer(room)
                    await finish_night(room)
                elif room.state == "day":
                    cancel_timer(room)
                    await phase_voting(room)
                elif room.state == "voting":
                    cancel_timer(room)
                    await resolve_vote(room)

            elif mt == "restart":
                if sid == room.host_sid and room.state == "gameover":
                    room.state = "lobby"
                    room.round = 0
                    room.timer_ends_at = 0
                    room._winner_cache = None
                    for p in room.players.values():
                        p.alive, p.is_mafia = True, False
                        p.footballer, p.hint, p.image = "", "", None
                        p.vote_for, p.ready = None, False
                    push_log(room, "🔄 Хост открыл новое лобби — собирайте команду!")
                    await sync_all(room)

    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.info("ws ended: %s", e)
    finally:
        if sid in room.players:
            gone = room.players.pop(sid)
            push_msg(room, "система", f"🚪 **{gone.name}** покинул комнату")
            if room.host_sid == sid and room.players:
                room.host_sid = next(iter(room.players))
            if not room.players:
                cancel_timer(room)
                ROOMS.pop(room.code, None)
            else:
                if room.state == "night" and gone.is_mafia:
                    push_log(room, f"🕶️ Мафиози {gone.name} выпал из игры")
                await sync_all(room)


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
