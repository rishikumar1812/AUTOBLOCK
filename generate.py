import os
import random
import time
from datetime import datetime

LOG_FOLDER = "logs"
os.makedirs(LOG_FOLDER, exist_ok=True)

# Create 20 DL files if not present
for dl in range(1, 21):

    file_path = os.path.join(
        LOG_FOLDER,
        f"SMA328_DL{dl:02d}.csv"
    )

    if not os.path.exists(file_path):
        open(file_path, "w").close()

print("Generator started...")

while True:

    # Pick random DL
    dl = random.randint(1, 20)

    file_path = os.path.join(
        LOG_FOLDER,
        f"SMA328_DL{dl:02d}.csv"
    )

    now = datetime.now()

    # 30% FAIL, 70% PASS
    result = random.choices(
        ["PASS", "FAIL"],
        weights=[70, 30]
    )[0]

    with open(file_path, "a") as f:

        f.write("#INIT\n")
        f.write(f"RESULT : {result}\n")
        f.write(f"TIME : {now.strftime('%H:%M:%S')}\n")
        f.write(f"JIG : JIG_{dl:02d}\n")
        f.write(f"ARRAY : ARRAY_{dl:02d}\n")
        f.write(f"DATE : {now.strftime('%Y/%m/%d')}\n\n")

    print(
        f"[{now.strftime('%H:%M:%S')}] "
        f"DL{dl:02d} -> {result}"
    )

    # Generate one record every 2 seconds
    time.sleep(2)