# rosey (`hosted` branch)

This is the **`hosted`** branch of the Rosey repo. It contains the SaaS
router code for [rosey.house](https://rosey.house) — the QR-code
onboarding funnel and per-household VM provisioning.

For the open-source **self-host** product (the agent that runs inside
each household VM, and that anyone can deploy on their own Fly account
or laptop), check out the `main` branch.

The two branches share zero source files; they live in one repo for
administrative convenience.

## Architecture

```
[Phones] ── Telegram ──▶ @RoseyHouseholdBot ──▶ POST /telegram
                                                    │
                                                    ▼
                                           [rosey-router Flask app]
                                                    │  chat_id known?
                                          ┌─────────┴─────────┐
                                        YES                   NO
                                          │                   │
                       forward via 6PN to │                   ▼
                       household VM       │          [onboarding FSM]
                                          ▼                   │ 6-step dialog
                              http://rosey-h-XXXX             │ (household name,
                                .internal:8080                │  members, tz, ...)
                                                              ▼
                                                     [provisioning]
                                                     flyctl: app create,
                                                     volume, secrets, deploy
                                                     from rosey-template
                                                     image (~30s)
```

The household VM image is built from `main` and published to
`registry.fly.io/rosey-template:<tag>`. This branch pulls from that
registry — there's no source-tree dependency between the two branches.

## Quick deploy

See `router/README.md` for full setup. Short version:

```bash
git checkout hosted
cd router

fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_BOT_USERNAME=RoseyHouseholdBot \
  TELEGRAM_WEBHOOK_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  ROSEY_INTERNAL_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  ROSEY_OPERATOR_TELEGRAM_ID=<your chat id> \
  FLY_API_TOKEN=<org token> \
  OPENAI_API_KEY=... \
  ROUTER_DRY_RUN=0 \
  --stage -a rosey-router

fly deploy --remote-only -a rosey-router

curl -sS -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d url=https://rosey-router.fly.dev/telegram \
  -d secret_token=$TELEGRAM_WEBHOOK_SECRET
```

Disable bot privacy mode in BotFather (`/setprivacy` → Disable).

## Tests

```bash
cd router && python -m unittest discover -s tests
```

## Contract with `main`

The router serializes a `HOUSEHOLD_TOML` Fly secret that the household
VM (built from `main`) reads at startup:

```toml
household_name = "The Tandons"
shopping_cadence = "weekly"
upfront_context = "we have a dog and 2 kids in school"

[[members]]
name = "Ankit"
telegram_id = "100"            # for known members
notes = ""

[[members]]
name = "Sarah"
telegram_username = "sarah_t"  # for v2 pre-rostered placeholders
notes = ""
```

`main`'s `household.py` accepts `telegram_id`, `telegram_username`, and
a legacy `phone` field. If you add or rename a field here, update
`household.py` on `main` too.
