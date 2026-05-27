# Rosey deploy + verification checklist — 2026-05-22

Everything changed this session and how to ship + verify it. Three deploy
targets; **do them in this order** (engine exposes the tools → token server
allows them → static site).

---

## 0. Pre-flight (already done / verify)

- [ ] `PICOVOICE_ACCESS_KEY` set on `rosey-voice-token` — `fly secrets list -a rosey-voice-token` (confirmed Deployed)
- [ ] `OPENAI_API_KEY`, `ROSEY_PWA_ORIGIN` set on `rosey-voice-token` (confirmed)
- [ ] `SCHEDULER_TZ=America/Los_Angeles` on `rosey` — `fly ssh console -a rosey -C "printenv SCHEDULER_TZ"` (confirmed)

---

## 1. Engine app `rosey` — `fly deploy -a rosey`

Carries the Python + Baileys-sidecar changes:

- `mcp_server.py` — new tools `update_reminder`, `delete_reminder`,
  `get_current_time`, `add_recurring_reminder`; readback section-boundary fix
  (`_pending_head` / `_split_head_tail` now match the scheduler's headers).
- `scheduler.py` — group-chat routing (`_is_group_chat` + reconcile); WhatsApp
  @-ping support (gated, off by default).
- `agent.py` + `tools.py` — `create_reminders` tool (deterministic creation).
- `reminder_builder.py` — NEW shared module (agent + voice both use it).
  ⚠️ The `Dockerfile` copies an **explicit list** of Python files (not `COPY .`),
  so any NEW module must be added to that list or the image ships without it and
  the agent crashes on import. `reminder_builder.py` has been added (line 31).
  When adding future modules, update the Dockerfile COPY list too.
- `channels.py` + `baileys/index.js` — `mentions` passthrough for WA @-pings.

```
fly deploy -a rosey
```

**Verify after deploy:**

- [ ] Voice / WhatsApp: "what time is it?" → real current time (not "I can't see it").
- [ ] "what are my reminders?" → shows ALL pending incl. the baby-exercise 3pm
      slot (the stray `## Pending` no longer hides it).
- [ ] "remind Sunanda 5 times a day between 9 and 5 daily" → confirms the exact
      slots back (e.g. "today 3:00 PM, 5:00 PM; tomorrow 9, 11, 1 — repeating daily").
- [ ] "cancel the baby exercise reminder" / "move the dentist reminder to 4pm" → works.
- [ ] A reminder created in the family group fires **into the group** (tagging
      the person), not a 1:1 DM.

---

## 2. Token server `rosey-voice-token` — `fly deploy -a rosey-voice-token`

- `voice-token-server/session_server.py` — allow-list now includes
  `get_current_time`, `add_recurring_reminder`, `update_reminder`,
  `delete_reminder`; voice system prompt updated to use them.

```
fly deploy -a rosey-voice-token
```

**Verify:** the voice tools above actually fire (they're allowed at session
creation). If a voice command says it "can't" do one of these, this deploy
didn't land or the engine (step 1) isn't exposing the tool yet.

---

## 3. Static site `website/` — manual Cloudflare upload

- `website/voice/app.js` — Porcupine **v4** wake word (ESM build); "Rosey stop"
  dismissal; 90s hard-timeout now resets each turn; voice tool list + prompt.
- `website/voice/index.html` — updated footer hint.
- `website/voice/models/Rosey.ppn` + `porcupine_params.pv` —
  ⚠️ **UNTRACKED IN GIT.** They must be included in the upload or the wake word
  404s. Confirm both files are in the `website/voice/models/` you upload.

**Verify:**

- [ ] Footer reads: *Say "Rosey" to wake me … say "Rosey stop" when you're done*.
- [ ] Under-orb text reads **'Say "Rosey" to wake me'** (= Porcupine initialized).
      If it says "Tap the orb", open DevTools console for `porcupine init failed`.
- [ ] Say "Rosey" → wakes. Say "Rosey stop" → sleep chime, back to grey.
- [ ] A back-and-forth conversation is no longer cut off at ~90 seconds.

---

## 4. Optional cleanup — the stray `## Pending` header

Harmless after step 1 (the readback fix reads past it), but to tidy the file.
Your earlier one-liner failed because `fly ssh -C` does **not** run a shell, so
`&&`, quotes, and `$` were passed literally to `cp`. Use an interactive shell:

```
fly ssh console -a rosey
# then, inside the machine's shell:
cd /data/memories
cp reminders.md reminders.md.bak
sed -i '/^## Pending$/d' reminders.md
exit
```

(or one-shot through an explicit shell:
`fly ssh console -a rosey -C "sh -lc 'cp /data/memories/reminders.md /data/memories/reminders.md.bak && sed -i /^##\\ Pending\$/d /data/memories/reminders.md'"`)

---

## 5. Optional — turn on real WhatsApp @-pings

Group reminders already deliver to the group; this makes the assignee a true
push @-mention instead of plain text. It's **off by default** because WhatsApp's
LID-vs-phone mention rendering can't be verified except against a live group.

1. Enable: `fly secrets set WHATSAPP_GROUP_MENTIONS=on -a rosey` (triggers a redeploy).
2. Live test: create a group reminder for one person and confirm they get a real
   @-ping (their name highlighted + a notification), not a raw `@<number>`.
3. If it shows a raw number, the LID form is off — tell me and I'll switch the
   JID domain (`@lid` ↔ `@s.whatsapp.net`) in `scheduler._wa_mention_for`.

Fail-safe: if anything doesn't resolve, reminders still send normally with the
name as text — no reminder is ever lost by this feature.

---

## What this session fixed (summary)

| Area | Change |
|---|---|
| Wake word | Porcupine enabled + aligned to v4 SDK (was silently broken) |
| Voice UX | "Rosey stop" dismissal; 90s cap resets per turn (no mid-convo cutoff) |
| Time | `get_current_time` tool (voice couldn't tell the time) |
| Readback | `## Pending`-style stray headers no longer hide live reminders |
| Editing | `update_reminder` / `delete_reminder` (incl. stop/▲change recurring) |
| Creation | Deterministic `create_reminders` / `add_recurring_reminder` — fixes the "5×/day" mess at the source |
| Routing | Group-created reminders fire into the group, tagging the person |
| WhatsApp | Real @-ping support (opt-in) |
