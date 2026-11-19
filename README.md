# Amazon Egypt Telegram watcher

GitHub Actions checks Amazon Egypt's public Deals page every five minutes. The current White Friday landing page is a direct Amazon fallback. Coverage is limited to product cards included in the page response and may change as Amazon updates the site. A missing or unreadable page fails the run. GitHub can delay runner starts.

The watcher computes discounts strictly above 70% from the EGP prices Amazon displays. It sends a direct Amazon product link for a new qualifying ASIN or a lower price than its last alert. Check purchase eligibility and shipping on Amazon. The monitor-state branch holds the alert history and polling offset. GitHub Actions Secrets hold the Telegram token and private chat ID.

Run a source-only check with `python3 watcher.py check`. Use the workflow's Run workflow button to trigger an immediate cloud check. The Telegram scan button requests a summary on the next poll.
