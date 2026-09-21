# Cliproxy Discord Monitor v2

This version sends the same IP Lists POST request identified from the
Cliproxy website Network tab:

POST https://api.cliproxy.com/v1/ipList

The bot monitors a three-octet prefix such as 172.58.132 for a selected
country and DMs the owner when Cliproxy returns at least one result.

## Setup

1. Install Python 3.13+.
2. Open Command Prompt in this folder.
3. Run: py -m pip install -r requirements.txt
4. Copy .env.example to .env.
5. Put your Discord bot token and Discord user ID into .env.
6. Put your own Cliproxy key and token into .env.
7. Run: py bot.py

Discord commands:
  /watch 172.58.132 US
  /list
  /stop 172.58.132 US

Keep .env private. Never post your Cliproxy key/token or Discord token.
