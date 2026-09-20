# GDELT Monitor Bluesky setup

The application is deployed in **review mode** first. Account creation and app-password
creation stay interactive so a personal password is never copied into a prompt, terminal
history, repository, or log.

## Account identity

- Display name: **GDELT Monitor**
- Final handle: **@gdeltmonitor.com**
- Bio:

  > Automated cross-source event signals from GDELT Monitor. Independent project using
  > GDELT data; not affiliated with the GDELT Project. AI-assisted explainers; every
  > signal links to its source set. Built and operated by Sidd Nambiar.

- Website: `https://gdeltmonitor.com/about`
- Apply Bluesky's bot/self-label in the account settings. If that control is unavailable,
  add `Automated account` to the first line of the bio.
- Use the existing GDELT Monitor globe artwork for the avatar and header.

Create the account with a temporary `bsky.social` handle, then use Bluesky's custom-domain
handle flow to verify `gdeltmonitor.com`. Prefer the DNS `_atproto` method. Confirm that
`com.atproto.identity.resolveHandle?handle=gdeltmonitor.com` returns the account DID before
changing any automation configuration.

## Host secrets

Create `data/.bluesky_bot` on the production host with ACLs limited to the scheduled-task
user and SYSTEM:

```dotenv
BLUESKY_HANDLE=gdeltmonitor.com
BLUESKY_APP_PASSWORD=<account-specific app password>
```

Create a separately attributed, spend-capped gateway virtual key and place only that key
in `data/.social_gateway_key`. Do not reuse `data/.openrouter_key`; separate attribution
and revocation are part of the safety boundary.

Neither secret file is tracked because the entire `data/` directory is gitignored. Never
put their contents in documentation, commits, issue comments, or logs.

## Activation

1. Register the worker with `scripts/register_social_task.ps1`. It uses an S4U principal,
   runs every 15 minutes, and immediately populates the review queue.
2. Sign in as the site admin and open `/admin/social`.
3. Keep mode set to `review` for at least 50 decisions over 2–3 weeks.
4. Run `python pipeline/social_publisher.py --calibrate`. Auto mode remains locked unless
   the chronological holdout, edit-rate, and recent-grounding gates pass.
5. Select `auto` in `/admin/social` only after calibration reports `HEALTHY`.

To stop all social work without deleting state, set the UI mode to `off`. To stop the OS
job too, disable `GDELT-SocialPublisher` in Task Scheduler.
