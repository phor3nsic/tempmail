/**
 * tempmail — Cloudflare Email Worker.
 *
 * Every inbound message for a configured zone lands here through the zone's
 * catch-all rule. The Worker decides whether the recipient is a live disposable
 * address, and if so parses the message, renders it safe, stores the sanitized
 * text in D1 for `tempmail inbox` / `read`, and notifies Slack.
 *
 * Operating assumption: the message is hostile. It is never executed, never
 * rendered as HTML, never fetched from, and never echoed back to its sender.
 *
 * Uploaded verbatim by the CLI — there is no build step.
 */

const SECOND = 1000;
const HOUR_SECONDS = 3600;

const DEFAULTS = {
  maxMessageBytes: 1048576,
  rateLimitPerHour: 20,
  stripPlusTag: true,
  slackTimeoutMs: 5000,
  storeMessages: true,
  maxStoredBodyChars: 16384,
  // The untouched HTML is kept alongside the stripped text so an operator (or a
  // model) can audit what the sender actually tried to render: tracking pixels,
  // masked anchors, hidden preheaders. It is never rendered and never sent to
  // Slack — `tempmail read --no-filter` prints it as inert text.
  storeHtml: true,
  maxStoredHtmlChars: 65536,
  retentionHours: 168,
};

// Hard ceilings the configuration cannot raise. These bound the work a single
// hostile message can make the Worker do.
const LIMITS = {
  maxSlackBodyChars: 2500,
  maxSlackPayloadBytes: 40000,
  maxSubjectChars: 200,
  maxAddressChars: 320,
  maxLinks: 5,
  maxCodes: 3,
  maxAttachments: 10,
  maxParts: 60,
  maxDepth: 6,
  maxHeaderBytes: 65536,
  maxEntityExpansions: 5000,
};

const OUTCOME = {
  DELIVERED: "delivered",
  UNKNOWN: "dropped_unknown",
  REVOKED: "dropped_revoked",
  EXPIRED: "dropped_expired",
  RATE_LIMITED: "rate_limited",
  TOO_LARGE: "too_large",
  PARSE_FALLBACK: "parse_fallback",
};

export default {
  async email(message, env, ctx) {
    // The handler never throws. A thrown error would surface to the sending
    // mail server as a temporary failure, turning this Worker into an oracle
    // for which addresses exist.
    try {
      await handleEmail(message, env, ctx);
    } catch (err) {
      console.log("tempmail: unhandled error:", describeError(err));
    }
  },
};

async function handleEmail(message, env, ctx) {
  const config = loadConfig(env);
  const envelopeTo = lower(trim(message.to));
  const resolved = resolveRecipient(envelopeTo, config);

  if (!resolved) {
    console.log("tempmail: undeliverable recipient shape");
    return;
  }

  const { email, domain } = resolved;
  const settings = domainSettings(config, domain);
  const now = nowSeconds();
  const from = lower(trim(message.from)).slice(0, LIMITS.maxAddressChars);
  const rawSize = Number(message.rawSize) || 0;

  const context = { env, email, rcpt: envelopeTo, from, rawSize, now };

  if (rawSize > settings.maxMessageBytes) {
    await logOutcome(context, OUTCOME.TOO_LARGE);
    return;
  }

  const address = await lookupAddress(env, email);

  if (!address) {
    // Silently accept and discard. Rejecting would confirm to a scanner which
    // local parts are live.
    await logOutcome(context, OUTCOME.UNKNOWN);
    return;
  }

  if (address.status === "revoked") {
    await logOutcome(context, OUTCOME.REVOKED);
    return;
  }

  if (address.expires_at && Number(address.expires_at) <= now) {
    // Expiry is enforced here, at delivery time, so it holds even if no cron
    // has run since the address lapsed.
    await markExpired(env, email, now);
    await logOutcome(context, OUTCOME.EXPIRED);
    return;
  }

  if (address.status !== "active") {
    await logOutcome(context, OUTCOME.UNKNOWN);
    return;
  }

  const rate = await consumeRateBudget(env, address, settings, now);
  if (!rate.allowed) {
    await logOutcome(context, OUTCOME.RATE_LIMITED);
    return;
  }

  let parsed;
  let outcome = OUTCOME.DELIVERED;
  try {
    const raw = await readRaw(message, settings.maxMessageBytes);
    parsed = parseMessage(raw);
  } catch (err) {
    // Malformed MIME must not cost us the notification: fall back to the
    // envelope and headers, and never ship the undecoded blob anywhere.
    console.log("tempmail: parse fallback:", describeError(err));
    parsed = fallbackParse(message);
    outcome = OUTCOME.PARSE_FALLBACK;
  }

  const record = buildRecord(parsed, context, settings, outcome);
  const messageId = await storeMessage(env, record, settings);
  record.id = messageId;

  const webhook = webhookFor(env, domain);
  if (webhook) {
    const status = await postToSlack(webhook, record, settings);
    if (messageId && status !== "ok") {
      ctx.waitUntil(updateSlackStatus(env, messageId, status));
    }
  }

  ctx.waitUntil(purgeExpiredMessages(env, settings, now));
}

/* ------------------------------------------------------------------ config */

function loadConfig(env) {
  let parsed = {};
  if (env.CONFIG) {
    try {
      parsed = JSON.parse(env.CONFIG) || {};
    } catch (err) {
      console.log("tempmail: CONFIG binding is not valid JSON; using defaults");
    }
  }
  return {
    defaults: Object.assign({}, DEFAULTS, parsed.defaults || {}),
    domains: parsed.domains || {},
  };
}

function domainSettings(config, domain) {
  return Object.assign({}, config.defaults, config.domains[domain] || {});
}

/**
 * Per-domain webhooks live in their own secret binding so one noisy domain can
 * be pointed at a different channel without touching the others.
 */
function webhookFor(env, domain) {
  const key = "SLACK_WEBHOOK_" + domain.replace(/[^a-z0-9]+/gi, "_").toUpperCase();
  return env[key] || env.SLACK_WEBHOOK_URL || "";
}

function resolveRecipient(rawRecipient, config) {
  // Normalised here as well as at the call site: addresses are stored
  // lowercase, so a mixed-case envelope that slipped through would miss the
  // lookup and the mail would vanish silently.
  const envelopeTo = lower(trim(rawRecipient));
  if (!envelopeTo || envelopeTo.indexOf("@") < 0) return null;

  const at = envelopeTo.lastIndexOf("@");
  let local = envelopeTo.slice(0, at);
  const domain = envelopeTo.slice(at + 1);
  if (!local || !domain) return null;

  const settings = domainSettings(config, domain);
  if (settings.stripPlusTag) {
    // Sub-addressing stays usable: x7k2p9+netflix@ resolves to x7k2p9@.
    local = local.split("+")[0];
  }
  if (!local) return null;

  return { email: local + "@" + domain, domain: domain, local: local };
}

/* ---------------------------------------------------------------------- D1 */

async function lookupAddress(env, email) {
  if (!env.DB) throw new Error("D1 binding DB is missing");
  return await env.DB.prepare(
    "SELECT email, domain, status, expires_at, rate_window_start, rate_window_count, " +
      "msg_count FROM addresses WHERE email = ?"
  )
    .bind(email)
    .first();
}

async function markExpired(env, email, now) {
  try {
    await env.DB.prepare(
      "UPDATE addresses SET status = 'expired' WHERE email = ? AND status = 'active'"
    )
      .bind(email)
      .run();
  } catch (err) {
    console.log("tempmail: could not mark expired:", describeError(err));
  }
}

/**
 * Fixed-window counter held on the address row. Two deliveries landing in the
 * same instant can both read the same count, so the cap is a guard against
 * floods rather than an exact quota.
 */
async function consumeRateBudget(env, address, settings, now) {
  const limit = Number(settings.rateLimitPerHour) || 0;
  if (limit <= 0) return { allowed: true };

  let windowStart = Number(address.rate_window_start) || 0;
  let count = Number(address.rate_window_count) || 0;

  if (now - windowStart >= HOUR_SECONDS) {
    windowStart = now;
    count = 0;
  }
  if (count >= limit) return { allowed: false };

  try {
    await env.DB.prepare(
      "UPDATE addresses SET rate_window_start = ?, rate_window_count = ?, " +
        "msg_count = msg_count + 1, last_msg_at = ? WHERE email = ?"
    )
      .bind(windowStart, count + 1, now, address.email)
      .run();
  } catch (err) {
    console.log("tempmail: rate counter update failed:", describeError(err));
  }
  return { allowed: true };
}

async function logOutcome(context, outcome) {
  try {
    await context.env.DB.prepare(
      "INSERT INTO messages (email, rcpt, from_addr, received_at, outcome, raw_size) " +
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
      .bind(
        context.email,
        context.rcpt,
        context.from,
        context.now,
        outcome,
        context.rawSize
      )
      .run();
  } catch (err) {
    console.log("tempmail: could not record outcome", outcome, describeError(err));
  }
}

async function storeMessage(env, record, settings) {
  if (!settings.storeMessages) {
    await logOutcome(
      {
        env: env,
        email: record.email,
        rcpt: record.rcpt,
        from: record.from,
        now: record.received_at,
        rawSize: record.raw_size,
      },
      record.outcome
    );
    return null;
  }

  try {
    const result = await env.DB.prepare(
      "INSERT INTO messages (email, rcpt, from_addr, from_name, subject, received_at, " +
        "sent_at, outcome, raw_size, body_text, body_html, links, codes, attachments) " +
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
      .bind(
        record.email,
        record.rcpt,
        record.from,
        record.from_name,
        record.subject,
        record.received_at,
        record.sent_at,
        record.outcome,
        record.raw_size,
        truncate(record.body, Number(settings.maxStoredBodyChars) || 0).text,
        record.body_html || null,
        JSON.stringify(record.links),
        JSON.stringify(record.codes),
        JSON.stringify(record.attachments)
      )
      .run();
    const meta = result && result.meta;
    return meta && meta.last_row_id ? meta.last_row_id : null;
  } catch (err) {
    console.log("tempmail: could not store message:", describeError(err));
    return null;
  }
}

async function updateSlackStatus(env, messageId, status) {
  try {
    await env.DB.prepare("UPDATE messages SET slack_status = ? WHERE id = ?")
      .bind(status, messageId)
      .run();
  } catch (err) {
    console.log("tempmail: could not record slack status:", describeError(err));
  }
}

async function purgeExpiredMessages(env, settings, now) {
  const hours = Number(settings.retentionHours) || 0;
  if (hours <= 0) return;
  try {
    await env.DB.prepare("DELETE FROM messages WHERE received_at < ?")
      .bind(now - hours * HOUR_SECONDS)
      .run();
  } catch (err) {
    console.log("tempmail: retention purge failed:", describeError(err));
  }
}

/* -------------------------------------------------------------- raw stream */

/** Read the raw message, stopping hard at `limit` bytes. */
async function readRaw(message, limit) {
  const reader = message.raw.getReader();
  const chunks = [];
  let total = 0;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value || !value.length) continue;

      if (total + value.length > limit) {
        chunks.push(value.subarray(0, limit - total));
        total = limit;
        break;
      }
      chunks.push(value);
      total += value.length;
    }
  } finally {
    try {
      await reader.cancel();
    } catch (err) {
      /* stream already closed */
    }
  }

  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

/* ------------------------------------------------------------- MIME parser */

/**
 * A deliberately small MIME reader: enough to pull a subject, a text body and
 * attachment names out of real-world mail, with every loop bounded. It is not a
 * general MIME implementation, and when it gives up the caller falls back to
 * headers only.
 *
 * Structural parsing happens on a byte-exact binary string, so a part's bytes
 * survive untouched until the charset decoder runs on them.
 */
function parseMessage(bytes) {
  const binary = bytesToBinary(bytes);
  const root = parsePart(binary, 0);

  const collected = { text: "", html: "", attachments: [] };
  walkPart(root, collected, 0, { parts: 0 });

  const headers = root.headers;
  return {
    subject: decodeWords(headerValue(headers, "subject")),
    from: parseAddress(decodeWords(headerValue(headers, "from"))),
    date: parseDate(headerValue(headers, "date")),
    text: collected.text,
    html: collected.html,
    attachments: collected.attachments,
  };
}

function parsePart(binary, depth) {
  const split = findHeaderEnd(binary);
  const headerBlock = binary.slice(0, split.end).slice(0, LIMITS.maxHeaderBytes);
  const body = binary.slice(split.bodyStart);
  const headers = parseHeaders(headerBlock);

  const contentType = parseContentType(headerValue(headers, "content-type"));
  const encoding = lower(trim(headerValue(headers, "content-transfer-encoding")));
  const disposition = parseContentType(headerValue(headers, "content-disposition"));

  const part = {
    headers: headers,
    mediaType: contentType.type || "text/plain",
    params: contentType.params,
    encoding: encoding,
    disposition: disposition.type || "",
    filename: disposition.params.filename || contentType.params.name || "",
    body: body,
    children: [],
  };

  if (part.mediaType.indexOf("multipart/") === 0 && depth < LIMITS.maxDepth) {
    const boundary = contentType.params.boundary;
    if (boundary) {
      for (const chunk of splitByBoundary(body, boundary)) {
        part.children.push(parsePart(chunk, depth + 1));
      }
    }
  }
  return part;
}

function walkPart(part, collected, depth, counter) {
  if (depth > LIMITS.maxDepth) return;
  if (counter.parts++ > LIMITS.maxParts) return;

  if (part.children.length) {
    const alternative = part.mediaType === "multipart/alternative";
    for (const child of part.children) {
      walkPart(child, collected, depth + 1, counter);
      // In an alternative set, the richest part we understand wins; keep
      // scanning only while we still have nothing.
      if (alternative && collected.text && collected.html) break;
    }
    return;
  }

  const isAttachment =
    part.disposition === "attachment" || (part.filename && part.disposition !== "inline");

  if (isAttachment) {
    if (collected.attachments.length < LIMITS.maxAttachments) {
      collected.attachments.push({
        name: sanitizeLine(decodeWords(part.filename) || "unnamed", 128),
        type: sanitizeLine(part.mediaType, 64),
        size: estimateSize(part.body, part.encoding),
      });
    }
    return;
  }

  if (part.mediaType === "text/plain" && !collected.text) {
    collected.text = decodeBody(part);
  } else if (part.mediaType === "text/html" && !collected.html) {
    collected.html = decodeBody(part);
  }
}

function decodeBody(part) {
  let bytes;
  const encoding = part.encoding;

  if (encoding === "base64") {
    bytes = decodeBase64(part.body);
  } else if (encoding === "quoted-printable") {
    bytes = binaryToBytes(decodeQuotedPrintable(part.body));
  } else {
    bytes = binaryToBytes(part.body);
  }

  const charset = part.params.charset || "utf-8";
  return decodeCharset(bytes, charset);
}

function decodeCharset(bytes, charset) {
  const label = String(charset || "utf-8").trim().replace(/^["']|["']$/g, "");
  try {
    return new TextDecoder(label, { fatal: false }).decode(bytes);
  } catch (err) {
    return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  }
}

function findHeaderEnd(binary) {
  const crlf = binary.indexOf("\r\n\r\n");
  const lf = binary.indexOf("\n\n");

  if (crlf >= 0 && (lf < 0 || crlf <= lf)) {
    return { end: crlf, bodyStart: crlf + 4 };
  }
  if (lf >= 0) {
    return { end: lf, bodyStart: lf + 2 };
  }
  return { end: binary.length, bodyStart: binary.length };
}

function parseHeaders(block) {
  const headers = [];
  // Unfold continuation lines before splitting on the field separator.
  const unfolded = block.replace(/\r?\n[ \t]+/g, " ");

  for (const line of unfolded.split(/\r?\n/)) {
    if (!line) continue;
    const colon = line.indexOf(":");
    if (colon <= 0) continue;
    headers.push({
      name: lower(line.slice(0, colon).trim()),
      value: decodeRawHeader(line.slice(colon + 1).trim()),
    });
  }
  return headers;
}

/**
 * Header values arrive as raw bytes. RFC 5322 says they are ASCII, but real
 * senders put unencoded UTF-8 in From and Subject, and reading those bytes as
 * latin1 would turn a bidi override into three harmless-looking characters that
 * slip straight past the sanitizer. Decode when the bytes really are UTF-8,
 * and leave them untouched when they are not.
 */
function decodeRawHeader(value) {
  if (!value || !/[\x80-\xff]/.test(value)) return value;
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(binaryToBytes(value));
  } catch (err) {
    return value;
  }
}

function headerValue(headers, name) {
  for (const header of headers) {
    if (header.name === name) return header.value;
  }
  return "";
}

function parseContentType(value) {
  const result = { type: "", params: {} };
  if (!value) return result;

  const segments = splitParams(value);
  result.type = lower(trim(segments.shift() || ""));

  for (const segment of segments) {
    const eq = segment.indexOf("=");
    if (eq <= 0) continue;
    const key = lower(trim(segment.slice(0, eq)));
    let raw = trim(segment.slice(eq + 1));
    if (raw.length > 1 && raw[0] === '"' && raw[raw.length - 1] === '"') {
      raw = raw.slice(1, -1);
    }
    result.params[key.replace(/\*$/, "")] = raw;
  }
  return result;
}

/** Split a header value on semicolons that are not inside a quoted string. */
function splitParams(value) {
  const out = [];
  let current = "";
  let quoted = false;

  for (let i = 0; i < value.length; i++) {
    const ch = value[i];
    if (ch === '"') {
      quoted = !quoted;
      current += ch;
    } else if (ch === ";" && !quoted) {
      out.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  out.push(current);
  return out;
}

function splitByBoundary(body, boundary) {
  const delimiter = "--" + boundary;
  const parts = [];
  let index = body.indexOf(delimiter);
  if (index < 0) return parts;

  index += delimiter.length;
  while (parts.length < LIMITS.maxParts) {
    if (body.slice(index, index + 2) === "--") break;

    const lineEnd = skipLineBreak(body, index);
    const next = body.indexOf(delimiter, lineEnd);
    if (next < 0) {
      parts.push(body.slice(lineEnd));
      break;
    }
    // Trim the CRLF that belongs to the delimiter, not to the part.
    let end = next;
    if (body[end - 1] === "\n") end--;
    if (body[end - 1] === "\r") end--;

    parts.push(body.slice(lineEnd, end));
    index = next + delimiter.length;
  }
  return parts;
}

function skipLineBreak(text, index) {
  if (text[index] === "\r") index++;
  if (text[index] === "\n") index++;
  return index;
}

function decodeQuotedPrintable(text) {
  return text
    .replace(/=\r?\n/g, "")
    .replace(/=([0-9A-Fa-f]{2})/g, function (match, hex) {
      return String.fromCharCode(parseInt(hex, 16));
    });
}

function decodeBase64(text) {
  const cleaned = text.replace(/[^A-Za-z0-9+/=]/g, "");
  try {
    return binaryToBytes(atob(cleaned));
  } catch (err) {
    // Tolerate a truncated final quantum rather than losing the whole body.
    const trimmed = cleaned.slice(0, cleaned.length - (cleaned.length % 4));
    try {
      return binaryToBytes(atob(trimmed));
    } catch (inner) {
      return new Uint8Array(0);
    }
  }
}

/** RFC 2047 encoded words: =?utf-8?q?Verify=20your=20email?= */
function decodeWords(value) {
  if (!value || value.indexOf("=?") < 0) return value || "";

  return value.replace(
    /=\?([^?]+)\?([bBqQ])\?([^?]*)\?=/g,
    function (match, charset, encoding, payload) {
      try {
        const kind = lower(encoding);
        const bytes =
          kind === "b"
            ? decodeBase64(payload)
            : binaryToBytes(decodeQuotedPrintable(payload.replace(/_/g, " ")));
        return decodeCharset(bytes, charset);
      } catch (err) {
        return match;
      }
    }
  );
}

function parseAddress(value) {
  const text = trim(value || "");
  const angle = text.match(/<([^>]*)>/);
  if (angle) {
    return {
      name: trim(text.slice(0, angle.index)).replace(/^["']|["']$/g, ""),
      email: lower(trim(angle[1])),
    };
  }
  return { name: "", email: lower(text) };
}

function parseDate(value) {
  if (!value) return null;
  const parsed = Date.parse(trim(value));
  if (isNaN(parsed)) return null;
  return Math.floor(parsed / SECOND);
}

function estimateSize(body, encoding) {
  if (encoding === "base64") return Math.floor((body.length * 3) / 4);
  return body.length;
}

function fallbackParse(message) {
  const headers = message.headers;
  const get = function (name) {
    try {
      return decodeWords(headers.get(name) || "");
    } catch (err) {
      return "";
    }
  };
  return {
    subject: get("subject"),
    from: parseAddress(get("from") || message.from),
    date: parseDate(get("date")),
    text: "",
    html: "",
    attachments: [],
  };
}

/* ------------------------------------------------------------- sanitizing */

// C0/C1 controls except tab and newline. These are what hide content from a
// reader while leaving it in the data.
const CONTROL_RE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g;
// Bidirectional overrides and invisible separators: the classic trick for
// making moc.live-evil@ look like live.com.
const INVISIBLE_RE = /[​-‏‪-‮⁠-⁤⁦-⁩﻿]/g;

function sanitizeText(value, max) {
  if (!value) return "";
  let text = String(value).replace(CONTROL_RE, "").replace(INVISIBLE_RE, "");
  text = text.replace(/\r\n?/g, "\n").replace(/\n{3,}/g, "\n\n");
  text = text.replace(/[ \t]{4,}/g, "   ").trim();
  return truncate(text, max).text;
}

function sanitizeLine(value, max) {
  return sanitizeText(value, max).replace(/\n+/g, " ");
}

function truncate(value, max) {
  const text = String(value || "");
  if (!max || text.length <= max) return { text: text, truncated: false };
  return {
    text: text.slice(0, max) + "\n[truncated, " + (text.length - max) + " more chars]",
    truncated: true,
  };
}

/**
 * Strip only what hides content from a reader — control characters and
 * bidirectional overrides — and leave every tag in place. This is the audit
 * copy: it is stored and printed as text, never parsed as HTML.
 */
function preserveMarkup(value, max) {
  if (!value) return "";
  const text = String(value)
    .replace(CONTROL_RE, "")
    .replace(INVISIBLE_RE, "")
    .replace(/\r\n?/g, "\n");
  return truncate(text, max).text;
}

const BLOCK_CLOSE_RE = /<\/(p|div|tr|li|h[1-6]|blockquote|table|section|article)\s*>/gi;
const DROP_ELEMENTS_RE =
  /<(script|style|head|iframe|object|embed|svg|noscript|template)\b[\s\S]*?<\/\1\s*>/gi;

function htmlToText(html) {
  if (!html) return "";

  let text = String(html)
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(DROP_ELEMENTS_RE, " ")
    // An unterminated script/style would otherwise leak its source as text.
    .replace(/<(script|style)\b[\s\S]*$/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<li\b[^>]*>/gi, "\n- ")
    .replace(BLOCK_CLOSE_RE, "\n")
    .replace(/<[^>]*>/g, " ");

  return decodeEntities(text);
}

const NAMED_ENTITIES = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  mdash: "—",
  ndash: "–",
  hellip: "…",
  trade: "™",
  copy: "©",
  reg: "®",
};

function decodeEntities(text) {
  let expansions = 0;
  return String(text).replace(/&(#x?[0-9a-f]+|[a-z]+);/gi, function (match, body) {
    if (expansions++ > LIMITS.maxEntityExpansions) return match;

    if (body[0] === "#") {
      const hex = body[1] === "x" || body[1] === "X";
      const code = parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10);
      if (!isFinite(code) || code <= 0 || code > 0x10ffff) return "";
      try {
        return String.fromCodePoint(code);
      } catch (err) {
        return "";
      }
    }
    const named = NAMED_ENTITIES[lower(body)];
    return named === undefined ? match : named;
  });
}

const URL_RE = /\bhttps?:\/\/[^\s<>"'`)\]}|\\]+/gi;

function extractLinks(text) {
  const seen = [];
  const matches = String(text || "").match(URL_RE) || [];

  for (const match of matches) {
    const url = match.replace(/[.,;:!?]+$/, "");
    if (url.length > 300) continue;
    if (seen.indexOf(url) < 0) seen.push(url);
    if (seen.length >= LIMITS.maxLinks) break;
  }
  return seen;
}

/** Render a URL unclickable: nobody opens a link from a hostile mail by accident. */
function defang(url) {
  return sanitizeLine(
    String(url).replace(/^http/i, "hxxp").replace(/\./g, "[.]"),
    320
  );
}

const CODE_KEYWORD_RE =
  /(code|otp|pin|token|password|passcode|verification|verify|auth|c[oó]digo|verifica[cç][aã]o|senha|acesso)/i;
const CODE_CANDIDATE_RE = /\b(\d{3}[- ]\d{3}|\d{4,8})\b/g;

/**
 * Pull likely one-time codes out of the body. Scored rather than filtered: a
 * digit run near the word "code" beats one that merely looks like a year.
 */
function extractCodes(text) {
  const body = String(text || "");
  const scored = [];
  let match;

  CODE_CANDIDATE_RE.lastIndex = 0;
  while ((match = CODE_CANDIDATE_RE.exec(body)) !== null) {
    const value = match[1];
    const digits = value.replace(/[^0-9]/g, "");
    const before = body.slice(Math.max(0, match.index - 60), match.index);
    const after = body.slice(
      match.index + value.length,
      match.index + value.length + 30
    );

    let score = 0;
    if (CODE_KEYWORD_RE.test(before)) score += 10;
    if (CODE_KEYWORD_RE.test(after)) score += 4;
    if (digits.length === 6) score += 3;
    else if (digits.length === 4 || digits.length === 8) score += 1;
    // A bare 4-digit number in the 19xx/20xx range is almost always a year.
    if (digits.length === 4 && /^(19|20)\d\d$/.test(digits) && score < 10) score -= 5;
    if (score <= 0) continue;

    scored.push({ value: digits, score: score, index: match.index });
    if (scored.length > 50) break;
  }

  scored.sort(function (a, b) {
    return b.score - a.score || a.index - b.index;
  });

  const out = [];
  for (const item of scored) {
    if (out.indexOf(item.value) < 0) out.push(item.value);
    if (out.length >= LIMITS.maxCodes) break;
  }
  return out;
}

/* ----------------------------------------------------------------- record */

function buildRecord(parsed, context, settings, outcome) {
  const bodySource = parsed.text || htmlToText(parsed.html);
  const body = sanitizeText(bodySource, Number(settings.maxStoredBodyChars) || 0);
  const html = settings.storeHtml
    ? preserveMarkup(parsed.html, Number(settings.maxStoredHtmlChars) || 0)
    : "";

  return {
    id: null,
    email: context.email,
    rcpt: context.rcpt,
    from: sanitizeLine(parsed.from.email || context.from, LIMITS.maxAddressChars),
    from_name: sanitizeLine(parsed.from.name || "", 128),
    subject: sanitizeLine(parsed.subject || "(no subject)", LIMITS.maxSubjectChars),
    received_at: context.now,
    sent_at: parsed.date,
    outcome: outcome,
    raw_size: context.rawSize,
    body: body,
    body_html: html,
    // Links are harvested from the markup as well, so a URL that only appears
    // in an href still shows up.
    links: extractLinks(bodySource + "\n" + html).map(function (url) {
      return sanitizeLine(url, 320);
    }),
    codes: extractCodes(bodySource),
    attachments: parsed.attachments,
  };
}

/* ------------------------------------------------------------------ Slack */

function slackPayload(record) {
  const received = formatTimestamp(record.received_at);
  const summary = truncate(record.body || "(empty body)", LIMITS.maxSlackBodyChars).text;

  const blocks = [
    {
      type: "header",
      text: { type: "plain_text", text: "\u{1F4E8} Temporary Email", emoji: true },
    },
    {
      type: "section",
      // plain_text objects are not parsed as mrkdwn, so a body containing
      // Slack formatting or a fake <http://...|link> stays inert.
      text: {
        type: "plain_text",
        text:
          "To:\n" +
          record.email +
          "\n\nFrom:\n" +
          (record.from_name ? record.from_name + " <" + record.from + ">" : record.from) +
          "\n\nSubject:\n" +
          record.subject +
          "\n\nReceived:\n" +
          received,
        emoji: false,
      },
    },
  ];

  if (record.codes.length) {
    // Codes are digits only by construction, so interpolating them into mrkdwn
    // cannot inject formatting.
    blocks.push({
      type: "section",
      text: {
        type: "mrkdwn",
        text:
          "*" +
          (record.codes.length > 1 ? "Codes:*  " : "Code:*  ") +
          record.codes
            .map(function (code) {
              return "`" + code + "`";
            })
            .join("   "),
      },
    });
  }

  blocks.push({ type: "divider" });
  blocks.push({
    type: "section",
    text: { type: "plain_text", text: summary || "(empty body)", emoji: false },
  });

  if (record.links.length) {
    blocks.push({
      type: "section",
      text: {
        type: "plain_text",
        text: "Links (defanged):\n" + record.links.map(defang).join("\n"),
        emoji: false,
      },
    });
  }

  if (record.attachments.length) {
    blocks.push({
      type: "context",
      elements: [
        {
          type: "plain_text",
          emoji: false,
          text:
            "Attachments (not forwarded): " +
            record.attachments
              .map(function (file) {
                return file.name + " (" + file.type + ", " + file.size + "B)";
              })
              .join(", "),
        },
      ],
    });
  }

  const footer = [
    record.id ? "tempmail read " + record.email + " --id " + record.id : "tempmail inbox " + record.email,
    record.raw_size + " bytes",
  ];
  if (record.outcome === OUTCOME.PARSE_FALLBACK) {
    footer.push("body could not be parsed; headers only");
  }
  blocks.push({
    type: "context",
    elements: [{ type: "plain_text", emoji: false, text: footer.join("  •  ") }],
  });

  return {
    text: "\u{1F4E8} New mail for " + record.email,
    blocks: blocks,
    unfurl_links: false,
    unfurl_media: false,
  };
}

async function postToSlack(webhook, record, settings) {
  let payload = slackPayload(record);
  let body = JSON.stringify(payload);

  if (body.length > LIMITS.maxSlackPayloadBytes) {
    // Shrink the one unbounded field rather than letting Slack reject the post.
    const shrunk = Object.assign({}, record, {
      body: truncate(record.body, 800).text,
      links: record.links.slice(0, 2),
    });
    payload = slackPayload(shrunk);
    body = JSON.stringify(payload);
  }

  try {
    const response = await fetch(webhook, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body,
      signal: AbortSignal.timeout(Number(settings.slackTimeoutMs) || DEFAULTS.slackTimeoutMs),
    });
    if (!response.ok) {
      // Slack echoes a short reason; the webhook URL itself is never logged.
      const reason = (await response.text()).slice(0, 120);
      console.log("tempmail: slack rejected the message:", response.status, reason);
      return "error_" + response.status;
    }
    return "ok";
  } catch (err) {
    console.log("tempmail: slack post failed:", describeError(err));
    return "error_network";
  }
}

/* ----------------------------------------------------------------- helpers */

function nowSeconds() {
  return Math.floor(Date.now() / SECOND);
}

function formatTimestamp(seconds) {
  if (!seconds) return "unknown";
  return new Date(seconds * SECOND).toISOString().replace("T", " ").slice(0, 19) + " UTC";
}

function lower(value) {
  return String(value || "").toLowerCase();
}

function trim(value) {
  return String(value || "").trim();
}

/** Error text for logs, with no message content and no secrets. */
function describeError(err) {
  if (!err) return "unknown";
  const name = err.name || "Error";
  const message = String(err.message || "").slice(0, 200);
  return name + ": " + message;
}

function bytesToBinary(bytes) {
  let out = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return out;
}

function binaryToBytes(binary) {
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    out[i] = binary.charCodeAt(i) & 0xff;
  }
  return out;
}

// Exported for the test harness; the Workers runtime only ever calls `email`.
export const __internals = {
  parseMessage,
  htmlToText,
  sanitizeText,
  sanitizeLine,
  preserveMarkup,
  extractCodes,
  extractLinks,
  defang,
  decodeWords,
  decodeRawHeader,
  decodeQuotedPrintable,
  resolveRecipient,
  buildRecord,
  slackPayload,
  truncate,
  webhookFor,
  loadConfig,
};
