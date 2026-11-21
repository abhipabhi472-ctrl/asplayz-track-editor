import os, signal, subprocess, sys, time

os.environ["API_ID"] = "25626879"
os.environ["API_HASH"] = "6624a2b7d641819dc13e1693b663e556"
os.environ["BOT_TOKEN"] = "9099811538:AAH_ffJ-s3AUsKAchEF7nY7vLr0EI7-q0mM"

# Stop any running bot.py process first
ps = subprocess.run(["ps", "aux"], capture_output=True, text=True)
for line in ps.stdout.splitlines():
    if "bot.py" in line and "python" in line:
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit():
            pid = int(parts[1])
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

BASE_DIR = "/content/asplayz-track-editor"
os.makedirs(os.path.join(BASE_DIR, "downloads"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "outputs"), exist_ok=True)

BOT_PATH = os.path.join(BASE_DIR, "bot.py")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

with open(LOG_PATH, "w", encoding="utf-8") as lf:
    proc = subprocess.Popen(
        [sys.executable, "-u", BOT_PATH],
        stdout=lf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

time.sleep(2)
print(f"RUNNING | PID={proc.pid} | EXIT={proc.poll()}")
with open(LOG_PATH, "r", encoding="utf-8") as rf:
    print(rf.read())
