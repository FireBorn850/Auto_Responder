import os, time, requests
from dotenv import load_dotenv

load_dotenv()
AUTH = (os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"])
BASE = "https://sandbox.dataforseo.com/v3"  # free mock data

payload = [{
    "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4",  # replace with a real place_id later
    "location_name": "Switzerland",
    "language_code": "en",
    "depth": 10,
    "sort_by": "newest",
}]

r = requests.post(f"{BASE}/business_data/google/reviews/task_post", json=payload, auth=AUTH, timeout=30)
print(r.status_code, r.json())
task_id = r.json()["tasks"][0]["id"]

task = r.json()["tasks"][0]
if task["status_code"] != 20100:  # 20100 = task created
    raise SystemExit(f"Task failed: {task['status_code']} {task['status_message']}")

for _ in range(12):
    time.sleep(10)
    g = requests.get(f"{BASE}/business_data/google/reviews/task_get/{task_id}", auth=AUTH, timeout=30)
    data = g.json()
    if data["tasks"][0].get("result"):
        for item in data["tasks"][0]["result"][0]["items"][:3]:
            print(item.get("profile_name"), item.get("rating", {}).get("value"), (item.get("review_text") or "")[:80])
        break
    print("waiting...", data["tasks"][0].get("status_message"))