#!/usr/bin/env node

import { chromium } from "playwright";

const BOOKING_URL = "https://booking.uz.gov.ua/";
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36";

function parseArguments() {
  const encoded = process.argv[2];
  if (!encoded) {
    throw new Error("Expected a base64-encoded JSON configuration argument");
  }
  return JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
}

async function searchDate(page, config, travelDate) {
  const apiResponses = [];
  const rememberTripUrl = (response) => {
    if (response.url().includes("app.uz.gov.ua/api/")) {
      apiResponses.push({ status: response.status(), url: response.url() });
    }
  };
  page.on("response", rememberTripUrl);
  try {
    const responsePromise = page.waitForResponse(
      (response) =>
        response.url().includes("app.uz.gov.ua/api/v3/trips") &&
        response.url().includes(`date=${travelDate}`),
      { timeout: 15_000 },
    );
    const routeUrl =
      `${BOOKING_URL}search-trips/${config.from_station_id}/` +
      `${config.to_station_id}/list?startDate=${travelDate}`;
    await page.goto(routeUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
    const response = await responsePromise;
    const payload = await response.json();
    const result = { travel_date: travelDate, status: response.status(), payload };
    const eligibleTrips = (payload.direct ?? []).filter((trip) => {
      if (!config.exact_seat_checks_enabled) {
        return false;
      }
      const coupe = (trip.train?.wagon_classes ?? []).find((wagon) =>
        ["\u041a", "K", "coupe"].includes(String(wagon.id)),
      );
      const freeSeats = Number(coupe?.free_seats ?? 0);
      const cacheKey = `${travelDate}|${trip.train?.number ?? "?"}`;
      return (
        freeSeats >= config.passengers &&
        (config.force_seat_check ||
          config.cached_availability?.[cacheKey] !== freeSeats)
      );
    });
    if (eligibleTrips.length > 0) {
      result.coupe_wagons_by_trip = {};
      result.seat_check_errors_by_trip = {};
      for (let index = 0; index < eligibleTrips.length; index += 1) {
        try {
          if (index > 0) {
            await page.goto(routeUrl, {
              waitUntil: "domcontentloaded",
              timeout: 30_000,
            });
          }
          const coupeButtons = page.getByRole("button", {
            name: new RegExp("^\\u041a\\u0443\\u043f\\u0435"),
          });
          const wagonResponsePromise = page.waitForResponse(
            (candidate) => candidate.url().includes("/wagons-by-class/"),
            { timeout: 15_000 },
          );
          await coupeButtons.nth(index).click({ timeout: 10_000 });
          const wagonResponse = await wagonResponsePromise;
          const tripId = wagonResponse
            .url()
            .match(/\/trips\/(\d+)\/wagons-by-class\//)?.[1];
          if (!tripId) {
            throw new Error(`cannot identify trip from ${wagonResponse.url()}`);
          }
          if (wagonResponse.ok()) {
            result.coupe_wagons_by_trip[tripId] = await wagonResponse.json();
          } else {
            const body = (await wagonResponse.text()).slice(0, 300);
            const verificationRequired =
              wagonResponse.status() === 443 && body.includes("content");
            result.seat_check_errors_by_trip[tripId] = verificationRequired
              ? "UZ account verification expired; exact seats unknown"
              : `wagon request returned ${wagonResponse.status()}: ${body}`;
          }
        } catch (error) {
          const tripId = String(eligibleTrips[index]?.id ?? `index-${index}`);
          result.seat_check_errors_by_trip[tripId] =
            error instanceof Error ? error.message : String(error);
        }
      }
    }
    return result;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(
      `${message}; page=${page.url()}; APIs=${JSON.stringify(apiResponses)}; ` +
        `body=${JSON.stringify((await page.locator("body").innerText()).slice(0, 300))}`,
    );
  } finally {
    page.off("response", rememberTripUrl);
  }
}

async function main() {
  const config = parseArguments();
  const context = await chromium.launchPersistentContext(config.user_data_dir, {
    headless: true,
    channel: "chrome",
    userAgent: USER_AGENT,
    locale: "uk-UA",
    timezoneId: "Europe/Warsaw",
  });
  try {
    const page = await context.newPage();
    const results = [];
    for (const [index, travelDate] of config.dates.entries()) {
      try {
        results.push(await searchDate(page, config, travelDate));
      } catch (error) {
        results.push({
          travel_date: travelDate,
          status: 0,
          error: error instanceof Error ? error.message : String(error),
        });
      }
      if (index + 1 < config.dates.length && config.request_delay_ms > 0) {
        const jitter = Math.random() * config.request_jitter_ms;
        await page.waitForTimeout(config.request_delay_ms + jitter);
      }
    }
    await context.storageState({ path: config.storage_state });
    process.stdout.write(JSON.stringify(results));
  } finally {
    await context.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  process.exitCode = 1;
});
