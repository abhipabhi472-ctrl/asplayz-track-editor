import os
import subprocess

!apt-get update -qq && apt-get install -y -qq ffmpeg
!pip install -q pyrogram tgcrypto aiohttp
!curl -fsSL https://raw.githubusercontent.com/abhipabhi472-ctrl/asplayz-track-editor/main/bot.py -o bot.py

os.environ["API_ID"] = "25626879"
os.environ["API_HASH"] = "6624a2b7d641819dc13e1693b663e556"
os.environ["BOT_TOKEN"] = "9099811538:AAH_ffJ-s3AUsKAchEF7nY7vLr0EI7-q0mM"
os.environ["GOFILE_TOKEN"] = ""

subprocess.run(["python", "-u", "bot.py"], check=True)

