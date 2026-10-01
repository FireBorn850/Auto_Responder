import os, time, requests
from dotenv import load_dotenv

load_dotenv()
AUTH = (os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"])
BASE = "https://sandbox.dataforseo.com/v3"  # free mock data
PLACE_ID = "ChIJN1t_tDeuEmsRUsoyG83frY4"

SB = os.environ["SUPABASE_URL"] + "/rest/v1/reviews"
HEAD = {
    "apikey": os.environ["SUPABASE_SERVICE_KEY"],
    "Authorization": "Bearer " + os.environ["SUPABASE_SERVICE_KEY"],
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

payload = [{
    "place_id": PLACE_ID,
    "location_name": "Switzerland",
    "language_code": "en",
    "depth": 10,
    "sort_by": "newest",
}]

r = requests.post(f"{BASE}/business_data/google/reviews/task_post", json=payload, auth=AUTH, timeout=30)
task = r.json()["tasks"][0]
if task["status_code"] != 20100:
    raise SystemExit(f"{task['status_code']}: {task['status_message']}")
task_id = task["id"]
print("Task created:", task_id)

for _ in range(12):
    time.sleep(10)
    g = requests.get(f"{BASE}/business_data/google/reviews/task_get/{task_id}", auth=AUTH, timeout=30)
    t = g.json()["tasks"][0]
    if t.get("result"):
        rows = []
        for item in t["result"][0]["items"]:
            rows.append({
                "review_id": item.get("review_id"),
                "place_id": PLACE_ID,
                "author": item.get("profile_name"),
                "rating": (item.get("rating") or {}).get("value"),
                "text": item.get("review_text"),
                "review_time": item.get("timestamp"),
            })
        res = requests.post(SB, json=rows, headers=HEAD, timeout=30)
        print("Supabase:", res.status_code, res.text[:200])
        break
    print("waiting...", t.get("status_message"))