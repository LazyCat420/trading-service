import json,logging
from datetime import datetime,timezone,timedelta
from collections import Counter,defaultdict
from app.db import mongo_store as m
from app.services.cycle_scope import is_synthetic_cycle
cutoff=datetime.now(timezone.utc)-timedelta(days=3)
rows=m.find_docs("agent_tool_telemetry",{"created_at":{"$gte":cutoff}},projection={"_id":0,"cycle_id":1,"agent_name":1,"tool_name":1,"args_hash":1,"success":1,"was_blocked":1,"elapsed_ms":1,"created_at":1})
groups=defaultdict(list)
for r in rows:
 if not is_synthetic_cycle(r.get("cycle_id")):groups[r.get("tool_name","unknown")].append(r)
out=[]
for tool,rs in groups.items():
 repeated=Counter((r.get("cycle_id"),r.get("agent_name"),r.get("args_hash")) for r in rs if r.get("args_hash"))
 out.append({"tool":tool,"calls":len(rs),"failures":sum(r.get("success") is False for r in rs),"blocked":sum(bool(r.get("was_blocked")) for r in rs),"repeated_same_role_cycle_hash":sum(n-1 for n in repeated.values()),"elapsed_ms":sum(r.get("elapsed_ms") or 0 for r in rs)})
print(json.dumps({"captured_at":datetime.now(timezone.utc).isoformat(),"cutoff":cutoff.isoformat(),"tools":sorted(out,key=lambda r:-r["calls"]),"caveat":"Repeated hash is a diagnostic, not proof of wasted calls: retries and changing whiteboard reads can be legitimate."},indent=2))
