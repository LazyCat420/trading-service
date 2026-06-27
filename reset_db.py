from pymongo import MongoClient
client = MongoClient('mongodb://admin:lazycat420@mongodb:27017/')
db = client.trading_db
print("Pipeline state update:", db.pipeline_state.update_one({}, {"$set": {"status": "idle"}}).modified_count)
print("v3_system_commands update:", db.v3_system_commands.update_many({"status": "running"}, {"$set": {"status": "error"}}).modified_count)
