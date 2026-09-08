import json,logging
from collections import defaultdict
from decimal import Decimal
from app.db import mongo_store as m
D=lambda v: Decimal(str(v or 0))
bots=m.find_docs("bots",{})
positions=m.find_docs("positions",{})
lots=m.find_docs("position_lots",{})
fills=m.find_docs("trade_fills",{})
orders=m.find_docs("orders",{})
order_ids={r.get("id") for r in orders}
fill_ids={r.get("fill_id") for r in fills}
lot_qty=defaultdict(Decimal)
for l in lots:lot_qty[(l.get("bot_id"),l.get("ticker"))]+=D(l.get("remaining_qty"))
issues=[]
for p in positions:
 key=(p.get("bot_id"),p.get("ticker"));qty=D(p.get("qty"))
 if abs(qty-lot_qty[key])>Decimal("0.00001"):issues.append({"kind":"position_lot_qty","bot":key[0],"ticker":key[1],"position":str(qty),"lots":str(lot_qty[key])})
order_map={r.get("id"):r for r in orders}
for f in fills:
 o=order_map.get(f.get("order_id"))
 if o and (o.get("ticker")!=f.get("ticker") or o.get("bot_id")!=f.get("bot_id") or o.get("side")!=f.get("side")):issues.append({"kind":"order_fill_identity","id":f.get("fill_id")})
 if f.get("order_id") not in order_ids:issues.append({"kind":"orphan_fill","id":f.get("fill_id")})
for l in lots:
 if l.get("fill_id") not in fill_ids:issues.append({"kind":"orphan_lot","id":l.get("lot_id")})
 if D(l.get("remaining_qty"))<0 or D(l.get("remaining_qty"))>D(l.get("original_qty")):issues.append({"kind":"invalid_lot_quantity","id":l.get("lot_id")})
cash=[]
for b in bots:
 bf=[f for f in fills if f.get("bot_id")==b.get("bot_id")]
 if not bf:continue
 net=sum((D(f.get("fill_value"))*(1 if f.get("side")=="SELL" else -1) for f in bf),Decimal(0))
 expected=D(b.get("starting_cash"))+net
 cash.append({"bot":b.get("bot_id"),"cash":str(b.get("cash_balance")),"starting_cash":str(b.get("starting_cash")),"ledger_expected":str(expected),"delta":str(D(b.get("cash_balance"))-expected),"fills":len(bf)})
print(json.dumps({"counts":{"bots":len(bots),"positions":len(positions),"lots":len(lots),"fills":len(fills),"orders":len(orders)},"field_keys":{"position":list(positions[0]) if positions else [],"order":list(orders[0]) if orders else []},"issues":issues,"cash_reconciliation":cash},default=str,indent=2))
