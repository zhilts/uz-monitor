#!/usr/bin/env node

import readline from "node:readline/promises";
import { chromium } from "playwright";

const config = JSON.parse(
  Buffer.from(process.argv[2], "base64url").toString("utf8"),
);

const context = await chromium.launchPersistentContext(config.user_data_dir, {
  headless: false,
  channel: "chrome",
  locale: "uk-UA",
  timezoneId: "Europe/Warsaw",
});

try {
  const page = await context.newPage();
  await page.goto(config.url, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const terminal = readline.createInterface({ input: process.stdin, output: process.stdout });
  await terminal.question(
    "Complete UZ account verification in Chrome, then press Enter here... ",
  );
  terminal.close();
} finally {
  await context.close();
}
