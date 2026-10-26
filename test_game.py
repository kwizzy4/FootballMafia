"""Интеграционный тест: 4 клиента играют полную партию через WebSocket."""
import asyncio
import json
import sys

import websockets

BASE = "ws://localhost:8000/ws"


async def recv_until(ws, pred, timeout=90, label=""):
    """Ждём сообщение, удовлетворяющее предикату."""
    end = asyncio.get_event_loop().time() + timeout
    while True:
        left = end - asyncio.get_event_loop().time()
        if left <= 0:
            raise AssertionError(f"timeout waiting {label}")
        raw = await asyncio.wait_for(ws.recv(), timeout=left)
        m = json.loads(raw)
        if pred(m):
            return m


def mk(name):
    async def run():
        ws = await websockets.connect(BASE)
        await ws.send(json.dumps({"op": "create" if name == "host" else "join",
                                  "name": name, "code": CODE[0]}))
        return ws
    return run


CODE = [""]


async def main():
    # хост создаёт комнату
    host = await websockets.connect(BASE)
    await host.send(json.dumps({"op": "create", "name": "Host"}))
    m = await recv_until(host, lambda x: x["type"] == "sync", label="join-host")
    CODE[0] = m["room"]["code"]
    print("room code:", CODE[0])

    guests = []
    for i in range(3):
        g = await websockets.connect(BASE)
        await g.send(json.dumps({"op": "join", "name": f"Guest{i}", "code": CODE[0]}))
        gm = await recv_until(g, lambda x: x["type"] == "sync", label=f"join-guest{i}")
        guests.append(g)
        assert len(gm["room"]["players"]) >= i + 2
    print("4 players in lobby OK")

    # старт игры
    await host.send(json.dumps({"type": "start"}))
    m = await recv_until(host, lambda x: x["type"] == "sync" and x["room"]["state"] == "night",
                         timeout=120, label="game-start-night")
    room, you = m["room"], m["you"]
    assert you["footballer"], "хост должен получить футболиста"
    assert you["hint"], "хост должен получить вики-подсказку"
    print(f"roles dealt. host: footballer={you['footballer']!r} mafia={you['isMafia']} hint={you['hint'][:70]!r}")
    for p in room["players"]:
        assert p["footballer"] is None or not (not p["revealedRole"]), "живые не должны раскрывать роль"
    alive_hidden = [p for p in room["players"] if p["alive"]]
    assert all(p["footballer"] is None for p in alive_hidden), "футболист живых скрыт!"

    # мафия находит себя среди клиентов и выбирает жертву
    all_ws = [host] + guests
    infos = {}
    for wsx in all_ws:
        mm = await recv_until(wsx, lambda x: x["type"] == "sync" and x["room"]["state"] == "night", label="night-sync")
        infos[wsx] = mm["you"]
    mafias = [w for w, y in infos.items() if y["isMafia"]]
    victims = [y["sid"] for y in infos.values() if not y["isMafia"]]
    print(f"mafia count: {len(mafias)}; mates visible: {mafias[0] and infos[mafias[0]]['mates']}")
    target = next(v for v in victims if v != infos[mafias[0]]["sid"])
    await mafias[0].send(json.dumps({"type": "kill", "target": target}))
    await recv_until(mafias[0], lambda x: x["type"] == "sync" and x["you"].get("nightTarget"), label="kill-confirm")
    print("mafia kill registered OK")

    # тайный чат мафии
    await mafias[0].send(json.dumps({"type": "chat", "text": "тихо, берём второго", "channel": "mafia"}))
    await recv_until(mafias[0], lambda x: x["type"] == "mafia_msg", label="mafia-chat")
    # мирный не должен получать мафиозные сообщения
    civ = [w for w in all_ws if not infos[w]["isMafia"]][0]
    try:
        got = await asyncio.wait_for(_try_recv(civ), 1.5)
        assert not (got and got.get("type") == "mafia_msg"), "мирный получил чат мафии!"
    except asyncio.TimeoutError:
        pass
    print("secret mafia chat isolated OK")

    # публичный чат
    await civ.send(json.dumps({"type": "chat", "text": "я забил 700 голов"}))
    await recv_until(host, lambda x: x["type"] == "chat" and "700" in x["msg"]["text"], label="public-chat")
    print("public chat OK")

    # хост пропускает ночь -> день
    await host.send(json.dumps({"type": "skip"}))
    md = await recv_until(host, lambda x: x["type"] == "sync" and x["room"]["state"] == "day", label="day-phase")
    dead = [p for p in md["room"]["players"] if not p["alive"]]
    assert len(dead) == 1, f"после ночи должен умереть 1, мертво {len(dead)}"
    assert md["room"]["lastKilled"] == dead[0]["name"]
    assert dead[0]["footballer"], "мёртвый должен раскрыть своего футболиста"
    print(f"night kill resolved: {dead[0]['name']} was playing {dead[0]['footballer']} OK")

    # голосование: все живые голосуют за одного
    await host.send(json.dumps({"type": "skip"}))
    mv = await recv_until(host, lambda x: x["type"] == "sync" and x["room"]["state"] == "voting", label="vote-phase")
    living = [p for p in mv["room"]["players"] if p["alive"]]
    victim_sid = living[-1]["sid"]
    voters = [w for w in all_ws if infos[w]["sid"] in [p["sid"] for p in living[:-1]]]
    for w in voters:
        await w.send(json.dumps({"type": "vote", "target": victim_sid}))
    mr = await recv_until(host, lambda x: x["type"] == "sync" and x["room"]["state"] in ("reveal", "gameover"),
                          timeout=60, label="vote-resolve")
    print("vote resolved ->", mr["room"]["state"], "| out:", mr["room"]["lastVotedOut"],
          "| wasMafia:", mr["room"]["lastVotedWasMafia"])
    assert mr["room"]["lastVotedOut"] == living[-1]["name"]

    # игра должна идти дальше или закончиться корректно — дождёмся следующей фазы/победы
    mn = await recv_until(host, lambda x: x["type"] == "sync" and x["room"]["state"] in ("night", "gameover"),
                          timeout=60, label="next-round")
    print("next phase:", mn["room"]["state"], "round:", mn["room"]["round"])

    # проверка ошибок входа
    bad = await websockets.connect(BASE)
    await bad.send(json.dumps({"op": "join", "name": "X", "code": "ZZZZZ"}))
    bm = await recv_until(bad, lambda x: x["type"] == "fatal", label="bad-code")
    print("wrong room code rejected:", bm["message"])

    for w in all_ws + [bad]:
        await w.close()
    print("\n✅ ALL GAME FLOWS PASSED")


async def _try_recv(ws):
    try:
        return json.loads(await ws.recv())
    except Exception:
        return None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("❌ FAIL:", e)
        sys.exit(1)
