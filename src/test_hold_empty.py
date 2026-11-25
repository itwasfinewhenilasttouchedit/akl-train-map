"""An empty feed must not blank the map; a real recovery must replace it."""
import asyncio, sys, time
sys.path.insert(0, "src")
import server

good = {"ts": int(time.time()) - 60, "count": 2, "poll_ms": 1, "trains": [{"unit": "1"}, {"unit": "2"}]}
empty = {"ts": int(time.time()), "count": 0, "poll_ms": 1, "trains": []}
seq = [empty, dict(good, ts=int(time.time()), count=3)]
fails = 0

async def fake_collect():
    return seq.pop(0)

class Stop(Exception): pass
async def fake_sleep(_):
    raise Stop

server.live.collect = fake_collect
server.asyncio.sleep = fake_sleep
server._annotate_consists = lambda: None
server._save_cache = lambda d: None
server.archive.get = lambda: None

def run():
    try: asyncio.run(server.poller())
    except Stop: pass

server.STATE.update(data=good, fetched=good["ts"], error=None)
run()
ok1 = server.STATE["data"] is good and "no vehicles" in (server.STATE["error"] or "")
run()
ok2 = server.STATE["data"]["count"] == 3 and server.STATE["error"] is None
server.STATE.update(data=dict(good, ts=int(time.time()) - 4000), error=None)
seq[:] = [empty]; run()
ok3 = server.STATE["data"]["count"] == 0
print("hold:", ok1, "recover:", ok2, "expire:", ok3)
sys.exit(0 if ok1 and ok2 and ok3 else 1)
