import json, logging
from datetime import datetime, timedelta, timezone
from app.db import mongo_store as s
since=datetime.now(timezone.utc)-timedelta(days=7)
base={'created_at':{'$gte':since},'cycle_id':{'$regex':'^cycle-v3-'}}
def agg(t,p): return s.aggregate(t,p)
result={'captured_at':datetime.now(timezone.utc).isoformat(),'window_days':7}
result['sections']=agg('whiteboard_entries',[{'$match':base},{'$group':{'_id':'$section','writes':{'$sum':1},'boards':{'$addToSet':{'ticker':'$ticker','cycle':'$cycle_id'}},'unknown_authors':{'$sum':{'$cond':[{'$in':['$author_agent',['unknown','']]},1,0]}}}},{'$project':{'_id':1,'writes':1,'boards':{'$size':'$boards'},'unknown_authors':1}},{'$sort':{'writes':-1}}])
result['annotations']=agg('whiteboard_annotations',[{'$match':base},{'$group':{'_id':'$section','count':{'$sum':1}}},{'$sort':{'count':-1}}])
result['duplicate_active_sections']=agg('whiteboard_entries',[{'$match':{**base,'superseded_by':None}},{'$group':{'_id':{'ticker':'$ticker','cycle':'$cycle_id','section':'$section'},'n':{'$sum':1}}},{'$match':{'n':{'$gt':1}}},{'$count':'count'}])
result['queue_status']=agg('v3_research_queues',[{'$group':{'_id':{'status':'$status','type':'$queue_type'},'count':{'$sum':1}}}])
result['annotation_revision_links']=agg('whiteboard_annotations',[{'$match':base},{'$lookup':{'from':s._coll('whiteboard_entries').name,'localField':'entry_id','foreignField':'id','as':'parent'}},{'$unwind':{'path':'$parent','preserveNullAndEmptyArrays':True}},{'$group':{'_id':{'$cond':[{'$ifNull':['$parent.superseded_by',False]},'superseded','current_or_missing']},'count':{'$sum':1}}}])
print(json.dumps(result,default=str,indent=2))
