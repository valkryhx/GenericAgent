from genericagent.llm.client import load_clients_from_yaml

clients, active, path, _ = load_clients_from_yaml()
c = clients[active]
print("using:", c.backend.name, c.backend.model)

msg = [{"role": "user", "content": "只回答三个字：能连通"}]
chunks = []
for ev in c.chat(msg):
    if isinstance(ev, str):
        chunks.append(ev)
    else:
        chunks.append(f"<{type(ev).__name__}> {repr(ev)[:200]}")
print("EVENTS:", len(chunks))
for ev in chunks:
    print("  ", ev[:300])
