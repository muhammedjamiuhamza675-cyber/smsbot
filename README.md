# SMS Bot

Telegram bot for textsms.io virtual number rentals.

## Deploy

Environment variables required:
- `TELEGRAM_BOT_TOKEN`
- `BOT_OWNER_ID`
- `TEXTSMS_KEY`
- `BOT_DATA_DIR=/data` (mount Railway volume at /data)
- `BOT_ADMIN_USERNAME` (optional, your Telegram handle)
