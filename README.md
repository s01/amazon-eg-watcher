# Amazon Egypt Telegram watcher

GitHub Actions checks the public SouqNile Amazon Egypt deals page every five minutes. GitHub can delay runner starts. Use the bot's scan button or /scan for a summary on the next poll. The source determines coverage, and the current price and shipping availability should be checked on Amazon.

The watcher computes discounts strictly above 70% from the displayed EGP reference and sale prices. It sends an alert for a new deal or a lower price than its last alert. The monitor-state branch holds product IDs, the last alerted prices and the Telegram polling offset. GitHub Actions Secrets hold the Telegram token and private chat ID.

Run a source-only check with `python3 watcher.py check`. Use the workflow's Run workflow button to trigger an immediate cloud check.
