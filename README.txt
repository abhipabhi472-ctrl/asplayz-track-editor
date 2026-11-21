Asplayz Track Editor Bot - GitHub & Colab Setup

1. Ee repo lo unna `bot.py`, `run_colab.py`, and `README.txt` files ni `abhipabhi472-ctrl/asplayz-track-editor` lo upload/update cheyyandi.
2. Google Colab lo `git pull` / `git clone` cell run chesthe automatic ga latest `bot.py` pull ayyi background lo start avthundi.
3. Features included:
   - Telegram file & direct HTTP/HTTPS URL streaming download (`aiohttp` real speed & `Content-Length` detection)
   - Audio track rename, reorder (UP/DN), and remove/restore
   - Subtitle add, remove, and MP4 `mov_text` conversion
   - Container conversion (MKV <-> MP4), output rename, and auto Gofile upload (>2 GB or user preference) with multi-server failover and real speed progress
