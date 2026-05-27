/**
 * Rosey Baileys sidecar.
 *
 * Why this exists:
 *   WhatsApp's Cloud API doesn't allow standard business accounts to use the
 *   Group API (it requires Official Business Account / blue tick, which is
 *   reserved for established brands). Baileys speaks WhatsApp's MultiDevice
 *   protocol directly — same wire protocol the WhatsApp mobile app uses —
 *   and CAN participate in user-created groups. The bot is "just another
 *   WhatsApp account" from Meta's perspective; it gets banned occasionally,
 *   but each ban only kills this account, not anyone else's.
 *
 * Architecture:
 *   Inbound (WhatsApp → Python):
 *     Baileys receives a message → we POST it as JSON to
 *     http://localhost:8080/whatsapp-baileys with the agreed schema. Python
 *     dispatches through the existing whatsapp_handler.handle_event flow.
 *
 *   Outbound (Python → WhatsApp):
 *     We expose a tiny HTTP server on localhost:3001 with a /send endpoint.
 *     Python's channels.send_whatsapp POSTs { to, text } when BAILEYS_MODE=on,
 *     we forward via Baileys, return the message id.
 *
 *   Authentication on loopback:
 *     Both endpoints check the BAILEYS_BRIDGE_SECRET env var via
 *     X-Bridge-Secret header. Loopback only, but defense in depth.
 *
 * Pairing:
 *   On first run, useMultiFileAuthState finds no creds and Baileys emits a
 *   `qr` event. We render the QR to the terminal AND log a guide URL the
 *   user can also scan. The user scans from any WhatsApp account (fresh
 *   number recommended — gets banned over time). Session creds save to
 *   BAILEYS_AUTH_DIR (defaults to /data/baileys-session/ on Fly).
 *
 * Reconnect:
 *   On disconnect we check the reason. Most disconnects are recoverable
 *   (Baileys auto-reconnects). The unrecoverable one is `loggedOut` — that's
 *   a ban or remote logout; we exit with code 1 and let the container
 *   restart. The /data session is invalid at that point and a human must
 *   re-pair.
 */

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  isJidGroup,
  isJidUser,
  downloadMediaMessage,
} = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const pino = require('pino');
const http = require('http');
const fs = require('fs');
const path = require('path');

// --- Config -------------------------------------------------------------------

const PYTHON_INBOUND_URL =
  process.env.PYTHON_INBOUND_URL || 'http://localhost:8080/whatsapp-baileys';
const BRIDGE_PORT = parseInt(process.env.BAILEYS_BRIDGE_PORT || '3001', 10);
const BRIDGE_SECRET = process.env.BAILEYS_BRIDGE_SECRET || '';
const AUTH_DIR = process.env.BAILEYS_AUTH_DIR || '/data/baileys-session';
// Hard cap on inbound image size we forward to Python (and onward to
// Anthropic). Anthropic's per-image limit is ~5MB base64-encoded, so we
// guard at ~4MB raw to leave headroom. Bigger images get dropped with a
// log line; the user gets a polite "image too large" reply via Python.
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
// Hard cap on inbound audio size. OpenAI Whisper's documented limit is
// 25MB; we cap at 16MB raw to leave headroom for the multipart upload.
// At typical WhatsApp Opus bitrate (~24kbps), 16MB is ~90 minutes of
// audio — well above any realistic voice memo.
const MAX_AUDIO_BYTES = 16 * 1024 * 1024;

if (!BRIDGE_SECRET) {
  // Refuse to start without a secret — the loopback HTTP server is
  // bound to 0.0.0.0:3001 inside the container and we don't want a
  // misconfigured deployment to be open.
  console.error(
    '[fatal] BAILEYS_BRIDGE_SECRET not set. Set it via fly secrets so Python and Node share the same value.'
  );
  process.exit(1);
}

const log = pino({ level: process.env.LOG_LEVEL || 'info' });

// --- Outbound HTTP bridge: Python POSTs here to send messages -----------------

let sock = null; // populated once the Baileys connection opens

/**
 * Convert our identifier flavor to a Baileys JID:
 *   wa:+15551234567        → 15551234567@s.whatsapp.net
 *   wa:group:120363xx@g.us → 120363xx@g.us (group JID kept verbatim)
 *   group:120363xx@g.us    → 120363xx@g.us (sometimes Python sends without wa:)
 *   raw JID                → returned unchanged
 */
function toJid(target) {
  if (!target) return null;
  let t = target.trim();
  if (t.startsWith('wa:')) t = t.slice(3);
  if (t.startsWith('group:')) t = t.slice(6);
  if (t.includes('@')) return t; // already a JID
  // Phone number form — strip +, append individual user suffix
  return `${t.replace(/^\+/, '')}@s.whatsapp.net`;
}

const bridge = http.createServer(async (req, res) => {
  // Auth check
  if (req.headers['x-bridge-secret'] !== BRIDGE_SECRET) {
    res.writeHead(403, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'forbidden' }));
    return;
  }
  if (req.method !== 'POST' || req.url !== '/send') {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not found' }));
    return;
  }

  let body = '';
  req.on('data', (chunk) => (body += chunk));
  req.on('end', async () => {
    try {
      const { to, text, mentions } = JSON.parse(body);
      if (!to || typeof text !== 'string') {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: "missing 'to' or 'text'" }));
        return;
      }
      if (!sock) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'baileys not connected yet' }));
        return;
      }
      const jid = toJid(to);
      // Optional `mentions`: array of JIDs to @-ping. The text must contain the
      // matching `@<user-part>` token(s) for WhatsApp to render the mention.
      // Backward compatible — when absent, behaves exactly as before.
      const msgContent = { text };
      if (Array.isArray(mentions) && mentions.length > 0) {
        msgContent.mentions = mentions;
      }
      const result = await sock.sendMessage(jid, msgContent);
      log.info({ to: jid, msgId: result?.key?.id }, 'outbound sent');
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ id: result?.key?.id, jid }));
    } catch (err) {
      log.error({ err: err.message }, 'send failed');
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message }));
    }
  });
});
bridge.listen(BRIDGE_PORT, '127.0.0.1', () => {
  log.info({ port: BRIDGE_PORT }, 'bridge listening on loopback');
});

// --- Inbound: post to Python ---------------------------------------------------

async function postToPython(payload) {
  const url = new URL(PYTHON_INBOUND_URL);
  const opts = {
    method: 'POST',
    hostname: url.hostname,
    port: url.port || (url.protocol === 'https:' ? 443 : 80),
    path: url.pathname,
    headers: {
      'Content-Type': 'application/json',
      'X-Bridge-Secret': BRIDGE_SECRET,
    },
  };
  return new Promise((resolve) => {
    const req = http.request(opts, (res) => {
      // Drain so we don't leak sockets
      res.on('data', () => {});
      res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', (err) => {
      log.warn({ err: err.message }, 'inbound forward failed');
      resolve(null);
    });
    req.write(JSON.stringify(payload));
    req.end();
  });
}

// --- Baileys connection --------------------------------------------------------

async function start() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  log.info({ version, authDir: AUTH_DIR }, 'starting baileys');

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false, // we render it ourselves below for log clarity
    // Don't sync the entire message history on connect — we don't need it
    // and it generates a lot of noise + load.
    syncFullHistory: false,
    // Mark ourselves as a normal client device. WhatsApp's anti-automation
    // ML weights "browser type" lightly; the safe choice is a real one.
    browser: ['Rosey', 'Desktop', '1.0.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      console.log('\n========== SCAN THIS QR FROM WHATSAPP ==========');
      qrcode.generate(qr, { small: true });
      console.log('================================================\n');
      log.info('QR ready — scan from the bot phone (WhatsApp → Linked Devices → Link a Device)');
    }
    if (connection === 'open') {
      log.info({ user: sock.user?.id }, 'baileys connected');
    }
    if (connection === 'close') {
      const reasonCode = lastDisconnect?.error?.output?.statusCode;
      const isLoggedOut = reasonCode === DisconnectReason.loggedOut;
      log.warn(
        { reasonCode, isLoggedOut, error: lastDisconnect?.error?.message },
        'connection closed'
      );
      if (isLoggedOut) {
        // Banned or manually unpaired. The session in /data is no longer
        // valid. Exit with non-zero so the container restarts and the
        // operator notices. Re-pairing requires a human to scan a new QR.
        log.error('logged out / banned — clearing session and exiting; human must re-pair');
        try {
          // Wipe the dead session so the next start triggers a fresh QR.
          for (const f of fs.readdirSync(AUTH_DIR)) {
            fs.unlinkSync(path.join(AUTH_DIR, f));
          }
        } catch (err) {
          log.error({ err: err.message }, 'failed to clear auth dir');
        }
        process.exit(1);
      } else {
        // Transient disconnect — restart the socket. Baileys handles the
        // reconnect handshake; we just call start() again.
        log.info('reconnecting in 2s');
        setTimeout(() => start().catch((e) => log.error({ e }, 'restart failed')), 2000);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return; // only fresh messages
    for (const m of messages) {
      // Skip messages we sent ourselves (Baileys echoes them back).
      if (m.key.fromMe) continue;
      // Skip status/broadcast updates — not real conversations.
      if (m.key.remoteJid === 'status@broadcast') continue;

      // Extract text. WhatsApp message bodies live in different fields
      // depending on type (regular text, extended text with quoted reply,
      // image with caption, etc.). We handle the common text-bearing ones.
      const imageMessage = m.message?.imageMessage || null;
      const audioMessage = m.message?.audioMessage || null;
      const text =
        m.message?.conversation ||
        m.message?.extendedTextMessage?.text ||
        imageMessage?.caption ||
        m.message?.videoMessage?.caption ||
        '';
      // Skip iff there's nothing actionable: no text, no image, no audio.
      // Video, locations, contacts etc. still get dropped here for v1.
      if (!text && !imageMessage && !audioMessage) {
        log.info({ msgId: m.key.id, type: Object.keys(m.message || {})[0] }, 'inbound non-text — skipping');
        continue;
      }

      // Compute the bot's own JID prefix(es) — used for both self-mention
      // rewriting (further down) and the audio-reply gate below. WhatsApp
      // accounts have both a legacy phone-number JID and a privacy-mode
      // "LID"; either can show up in mentioned/quoted refs depending on
      // the chat type.
      const selfIds = new Set();
      if (sock.user?.id) {
        selfIds.add(sock.user.id.split('@')[0].split(':')[0]);
      }
      if (sock.user?.lid) {
        selfIds.add(sock.user.lid.split('@')[0].split(':')[0]);
      }

      // If the message carries an image, download it via Baileys' built-in
      // media downloader and base64-encode for forwarding to Python.
      // Audio/video/document media are deliberately not handled here —
      // only images, which Claude can read natively via vision.
      let imageB64 = null;
      let imageMime = null;
      if (imageMessage) {
        try {
          const buf = await downloadMediaMessage(m, 'buffer', {}, {
            // Baileys needs a logger and the socket's reuploadRequest in
            // case the original media is no longer cached on WhatsApp's
            // servers and has to be re-requested.
            logger: log,
            reuploadRequest: sock.updateMediaMessage,
          });
          if (buf && buf.length > MAX_IMAGE_BYTES) {
            log.warn(
              { msgId: m.key.id, bytes: buf.length, cap: MAX_IMAGE_BYTES },
              'image exceeds size cap — dropping image, keeping caption only'
            );
          } else if (buf && buf.length > 0) {
            imageB64 = buf.toString('base64');
            imageMime = imageMessage.mimetype || 'image/jpeg';
            log.info(
              { msgId: m.key.id, bytes: buf.length, mime: imageMime },
              'downloaded inbound image'
            );
          }
        } catch (err) {
          // Don't drop the whole message — the caption (if any) is still
          // worth forwarding so the user gets some kind of response.
          log.warn(
            { msgId: m.key.id, err: err.message },
            'image download failed — forwarding caption only'
          );
        }
        // If after all that we have neither text nor image bytes, bail out
        // rather than send Python an empty payload.
        if (!text && !imageB64 && !audioMessage) {
          log.info({ msgId: m.key.id }, 'image-only message had no caption and download failed — skipping');
          continue;
        }
      }

      // Audio handling: download Opus/MP3/M4A voice notes for Whisper.
      //
      // Group gate: only forward audio when it's a quoted reply to one
      // of the bot's own messages. There's no audio @-mention to detect,
      // so without this gate every family voice note in the group would
      // burn a Whisper call. Mirrors the Telegram path's
      // `_voice_addressed_in_group` reply-to-bot trigger.
      //
      // DMs: always process — the user clearly intended to address the
      // bot when they sent a voice note 1-on-1.
      let audioB64 = null;
      let audioMime = null;
      if (audioMessage) {
        let allowAudio = true;
        if (isJidGroup(m.key.remoteJid)) {
          const quotedParticipant = audioMessage.contextInfo?.participant || '';
          const quotedId = quotedParticipant.split('@')[0].split(':')[0];
          const isReplyToBot = quotedId && selfIds.has(quotedId);
          if (!isReplyToBot) {
            log.info(
              { msgId: m.key.id },
              'group audio note is not a reply to bot — skipping'
            );
            allowAudio = false;
          }
        }
        if (allowAudio) {
          try {
            const buf = await downloadMediaMessage(m, 'buffer', {}, {
              logger: log,
              reuploadRequest: sock.updateMediaMessage,
            });
            if (buf && buf.length > MAX_AUDIO_BYTES) {
              log.warn(
                { msgId: m.key.id, bytes: buf.length, cap: MAX_AUDIO_BYTES },
                'audio exceeds size cap — dropping'
              );
            } else if (buf && buf.length > 0) {
              audioB64 = buf.toString('base64');
              audioMime = audioMessage.mimetype || 'audio/ogg; codecs=opus';
              log.info(
                { msgId: m.key.id, bytes: buf.length, mime: audioMime },
                'downloaded inbound audio'
              );
            }
          } catch (err) {
            log.warn(
              { msgId: m.key.id, err: err.message },
              'audio download failed — skipping'
            );
          }
        }
        // If nothing else is on the message and audio fell through, bail.
        if (!text && !imageB64 && !audioB64) {
          continue;
        }
      }

      // Resolve self-mentions before forwarding. When a user does a formal
      // @-mention of the bot in WhatsApp, the displayed text shows "@Rosey"
      // but the raw text payload Baileys exposes is `@<digits>` — either
      // the bot's phone (legacy) or LID (current). Python's gate looks for
      // the literal prefix "rosey" / "@rosey", so without this rewrite
      // formal @-mentions of the bot get dropped by the gate.
      //
      // Strategy: pull mentionedJid from the message's contextInfo, check
      // if any mention matches the bot's own phone or LID, and replace
      // the `@<digits>` token in the text with the literal "@rosey".
      //
      // WhatsApp puts mentionedJid on whichever message type carries the
      // text body: extendedTextMessage for plain text, imageMessage /
      // videoMessage for captioned media. Without checking all three, a
      // formal @-mention of the bot on a captioned photo never gets
      // rewritten to "@rosey", and the Python group gate then drops the
      // message silently because it doesn't see a rosey-prefix in the
      // text (only the raw `@<bot-digits>` left by WhatsApp).
      let processedText = text;
      const mentionedJids =
        m.message?.extendedTextMessage?.contextInfo?.mentionedJid ||
        m.message?.imageMessage?.contextInfo?.mentionedJid ||
        m.message?.videoMessage?.contextInfo?.mentionedJid ||
        [];
      let rewrote = false;
      for (const jid of mentionedJids) {
        const id = jid.split('@')[0].split(':')[0];
        if (selfIds.has(id)) {
          processedText = processedText.replace(
            new RegExp(`@${id}\\b`, 'g'),
            '@rosey'
          );
          rewrote = true;
        }
      }
      if (rewrote) {
        log.info(
          { msgId: m.key.id, original_len: text.length, processed_len: processedText.length },
          'rewrote bot @-mention digits to @rosey'
        );
      }

      // Detect quoted-reply-to-bot: when a user swipe-replies to one of
      // Rosey's messages, WhatsApp puts the quoted message's sender in
      // contextInfo.participant. Surface this to Python so the group
      // gate can auto-allow the reply (without it, casual replies like
      // "got it, give me some recs" get dropped because they don't
      // start with "rosey"). Matches the natural threading users do.
      const replyParticipant =
        m.message?.extendedTextMessage?.contextInfo?.participant ||
        m.message?.audioMessage?.contextInfo?.participant ||
        m.message?.imageMessage?.contextInfo?.participant || '';
      const replyParticipantId = replyParticipant.split('@')[0].split(':')[0];
      const isReplyToBot = !!replyParticipantId && selfIds.has(replyParticipantId);
      if (isReplyToBot) {
        log.info({ msgId: m.key.id }, 'quoted reply to bot — will bypass gate');
      }

      const remoteJid = m.key.remoteJid; // group JID or DM JID
      const isGroup = isJidGroup(remoteJid);
      // For group messages, m.key.participant tells us who actually sent it.
      // For DMs, the sender IS the remoteJid.
      const senderJid = isGroup ? m.key.participant : remoteJid;
      const senderPhone = senderJid?.split('@')[0]; // strip the @s.whatsapp.net

      const payload = {
        message_id: m.key.id,
        sender_phone: senderPhone,           // E.164 without leading +
        sender_jid: senderJid,
        chat_jid: remoteJid,                 // where to reply (group or DM)
        is_group: !!isGroup,
        text: processedText,
        timestamp: m.messageTimestamp,
        // Pass a hint to Python on which name to attribute, if available
        push_name: m.pushName || null,
        // Optional image attachment — Python forwards these straight into
        // Anthropic's vision input alongside the text body.
        image_b64: imageB64,
        image_mime: imageMime,
        // Optional audio attachment — Python pipes through Whisper and
        // uses the transcript as the message text for the agent.
        audio_b64: audioB64,
        audio_mime: audioMime,
        // True when this message is a quoted reply to one of Rosey's
        // own messages — Python bypasses the group gate in that case.
        is_reply_to_bot: isReplyToBot,
      };
      log.info(
        {
          sender: senderPhone,
          group: isGroup,
          len: text.length,
          has_image: !!imageB64,
          has_audio: !!audioB64,
          reply_to_bot: isReplyToBot,
        },
        'inbound msg'
      );
      const status = await postToPython(payload);
      if (status !== 200) {
        log.warn({ status }, 'python did not accept inbound');
      }
    }
  });
}

start().catch((err) => {
  log.error({ err: err.message, stack: err.stack }, 'baileys start failed');
  process.exit(1);
});

// Clean shutdown so SIGTERM from Fly doesn't leave the bridge port open
process.on('SIGTERM', () => {
  log.info('SIGTERM received, shutting down');
  bridge.close();
  process.exit(0);
});
process.on('SIGINT', () => {
  log.info('SIGINT received, shutting down');
  bridge.close();
  process.exit(0);
});
