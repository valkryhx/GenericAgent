import sys
from genericagent.llm.client import load_clients_from_yaml

clients, active, path, _ = load_clients_from_yaml()
c = clients[active]
print("using:", c.backend.name, c.backend.model)

msg = [{"role": "user", "content": "只回答三个字：能连通"}]
out = c.chat(msg)
print("RESP:", repr(out)[:600])
