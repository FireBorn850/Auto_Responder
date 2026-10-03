import os, time, requests
from dotenv import load_dotenv

load_dotenv()
AUTH = (os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"])
BASE = "https://api.dataforseo.com/v3"   # real API (the sandbox only returns fake data)

NAME = "Wolfisberg" # restaurant name to search for
URL_PATH = ""               # optional: paste e.g. Restaurant_Review-g188057-d...-Reviews-....html

task = {"depth": 10, "sort_by": "most_recent", "translate_reviews": False, "priority": 2}
if URL_PATH:
    task["url_path"] = URL_PATH
else:
    task["keyword"] = NAME
    task["location_name"] = "Geneva,Geneva,Switzerland"

body = requests.post(f"{BASE}/business_data/tripadvisor/reviews/task_post", json=[task], auth=AUTH, timeout=30).json()
if not body.get("tasks"):
    print("POST failed:", body.get("status_code"), body.get("status_message"))
    raise SystemExit
created = body["tasks"][0]
print("POST:", created["status_code"], created["status_message"])
if created["status_code"] != 20100:
    raise SystemExit

for _ in range(24):
    time.sleep(5)
    got = requests.get(f"{BASE}/business_data/tripadvisor/reviews/task_get/{created['id']}", auth=AUTH, timeout=30).json()["tasks"][0]
    print("GET:", got["status_code"], got["status_message"])
    if got.get("result"):
        res = got["result"][0]
        print("title:", res.get("title"))
        print("url_path:", res.get("url_path"))
        print("reviews_count:", res.get("reviews_count"), "| items_count:", res.get("items_count"))
        items = res.get("items") or []
        if items:
            print("fields in a review:", sorted(items[0].keys()))
            print("user_profile:", items[0].get("user_profile"))
            print("rating:", items[0].get("rating"), "| language:", items[0].get("language"), "/", items[0].get("original_language"))
        break
    if got["status_code"] not in (20000, 40601, 40602):
        break