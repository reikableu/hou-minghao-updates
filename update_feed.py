import json
from datetime import datetime
from zoneinfo import ZoneInfo


def main():
    singapore_time = datetime.now(
        ZoneInfo("Asia/Singapore")
    ).isoformat(timespec="seconds")

    try:
        with open("posts.json", "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        data = {
            "lastUpdated": None,
            "status": "Initial setup",
            "posts": []
        }

    data["lastUpdated"] = singapore_time
    data["status"] = "Daily updater completed successfully"

    with open("posts.json", "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

    print("posts.json updated successfully")
    print("Update time:", singapore_time)


if __name__ == "__main__":
    main()
