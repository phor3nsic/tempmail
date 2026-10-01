/**
 * Test harness for the Email Worker.
 *
 * Node only supplies the runtime here: it parses a fixture with the Worker's
 * own functions and prints the result as JSON, so the assertions can live in
 * pytest next to the rest of the suite.
 *
 *   node worker_harness.mjs <mode> <fixture.eml>
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const workerPath = join(here, "..", "src", "tempmail", "worker", "tempmail_worker.mjs");
const { __internals } = await import(workerPath);

const SETTINGS = {
  maxMessageBytes: 1048576,
  rateLimitPerHour: 20,
  stripPlusTag: true,
  storeMessages: true,
  maxStoredBodyChars: 16384,
  storeHtml: true,
  maxStoredHtmlChars: 65536,
  retentionHours: 168,
  slackTimeoutMs: 5000,
};

const [, , mode, fixture] = process.argv;

function parsed() {
  return __internals.parseMessage(new Uint8Array(readFileSync(fixture)));
}

function record() {
  const context = {
    email: "k8x4p2m9@example.com",
    rcpt: "k8x4p2m9@example.com",
    from: "envelope@service.test",
    rawSize: readFileSync(fixture).length,
    now: 1759258800,
  };
  return __internals.buildRecord(parsed(), context, SETTINGS, "delivered");
}

const modes = {
  parse: () => {
    const result = parsed();
    return {
      subject: result.subject,
      from: result.from,
      date: result.date,
      text: result.text,
      html: result.html,
      attachments: result.attachments,
    };
  },
  record: () => record(),
  slack: () => __internals.slackPayload(Object.assign({ id: 7 }, record())),
  recipient: () => {
    const config = __internals.loadConfig({
      CONFIG: JSON.stringify({ defaults: SETTINGS, domains: {} }),
    });
    return (fixture || "").split(",").map((value) => ({
      input: value,
      resolved: __internals.resolveRecipient(value, config),
    }));
  },
  limits: () => {
    const huge = "A".repeat(200000);
    return {
      truncated: __internals.truncate(huge, 1000).text.length,
      sanitizedLength: __internals.sanitizeText(huge, 2500).length,
      manyLinks: __internals.extractLinks(
        Array.from({ length: 50 }, (_, i) => "https://x" + i + ".test/a").join(" ")
      ).length,
      deepCodes: __internals.extractCodes("code 111111 code 222222 code 333333 code 444444").length,
    };
  },
};

if (!modes[mode]) {
  console.error("unknown mode: " + mode);
  process.exit(2);
}

process.stdout.write(JSON.stringify(modes[mode](), null, 2));
