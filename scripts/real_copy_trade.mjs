#!/usr/bin/env node
/**
 * Real Copy Trading Bot - Copies cigarettes wallet
 * $2 fixed bets on Polymarket
 */

console.log("=== REAL TRADING BOT STARTING ===");
console.log("Time:", new Date().toISOString());

import { ClobClient, Side, OrderType } from "@polymarket/clob-client";
import { Wallet } from "ethers";
import { createServer } from "http";

console.log("Dependencies loaded");

// Health check server for Railway
const PORT = process.env.PORT || 9092;
const server = createServer((req, res) => {
  if (req.url === "/health" || req.url === "/") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      status: "running",
      dailyTrades,
      dailyPnL,
      seenTrades: seenTrades.size,
      uptime: process.uptime()
    }));
  } else {
    res.writeHead(404);
    res.end("Not found");
  }
});
server.listen(PORT, () => console.log(`Health server on port ${PORT}`));

const HOST = "https://clob.polymarket.com";
const CHAIN_ID = 137;
const DATA_API = "https://data-api.polymarket.com";
const GAMMA_API = "https://gamma-api.polymarket.com";

// Config from environment (for Railway) or defaults
const PRIVATE_KEY = process.env.POLY_PRIVATE_KEY || "***REDACTED_COMPROMISED_KEY***";
const PROXY_WALLET = process.env.POLY_PROXY_WALLET || "0x80d96106879899b5fb2f7e232dc16cbb1215550c";
const TARGET_WALLET = process.env.TARGET_WALLET || "0xd218e474776403a330142299f7796e8ba32eb5c9"; // cigarettes
const BET_SIZE = parseFloat(process.env.BET_SIZE || "2"); // $2 per trade
const POLL_INTERVAL = parseInt(process.env.POLL_INTERVAL || "15000"); // 15 seconds
const MAX_DAILY_TRADES = parseInt(process.env.MAX_DAILY_TRADES || "50");
const MAX_DAILY_LOSS = parseFloat(process.env.MAX_DAILY_LOSS || "20"); // Stop if down $20

// State
let client = null;
let seenTrades = new Set();
let dailyTrades = 0;
let dailyPnL = 0;
let startBalance = 136;

async function initClient() {
  const signer = new Wallet(PRIVATE_KEY);
  const initClient = new ClobClient(HOST, CHAIN_ID, signer);
  const creds = await initClient.createOrDeriveApiKey();

  client = new ClobClient(
    HOST, CHAIN_ID, signer, creds,
    1, // POLY_PROXY
    PROXY_WALLET
  );

  console.log("✅ Client initialized");
  console.log(`   Owner: ${signer.address}`);
  console.log(`   Proxy: ${PROXY_WALLET}`);
}

async function fetchTargetTrades() {
  try {
    const res = await fetch(`${DATA_API}/trades?user=${TARGET_WALLET}&limit=20`);
    if (!res.ok) return [];
    return await res.json();
  } catch (e) {
    console.log(`⚠ Failed to fetch trades: ${e.message}`);
    return [];
  }
}

async function getMarketInfo(tokenId) {
  try {
    const market = await client.getMarket(tokenId);
    return market;
  } catch (e) {
    return null;
  }
}

async function placeBet(tokenId, side, price, marketName) {
  if (dailyTrades >= MAX_DAILY_TRADES) {
    console.log("⚠ Daily trade limit reached");
    return null;
  }

  if (dailyPnL <= -MAX_DAILY_LOSS) {
    console.log("🛑 Daily loss limit reached - stopping");
    return null;
  }

  const shares = Math.floor(BET_SIZE / price);
  if (shares < 1) {
    console.log(`⚠ Price too high for $${BET_SIZE} bet`);
    return null;
  }

  console.log(`\n📈 Placing ${side}: ${shares} shares @ ${price}`);
  console.log(`   Market: ${marketName?.slice(0, 50)}...`);

  try {
    const market = await getMarketInfo(tokenId);
    if (!market) {
      console.log("⚠ Could not get market info");
      return null;
    }

    const order = await client.createAndPostOrder(
      {
        tokenID: tokenId,
        price: price,
        size: shares,
        side: side === "BUY" ? Side.BUY : Side.SELL,
      },
      {
        tickSize: market.minimum_tick_size || "0.01",
        negRisk: market.neg_risk || false,
      },
      OrderType.GTC
    );

    console.log(`✅ Order: ${order.orderID} - ${order.status}`);
    dailyTrades++;
    return order;
  } catch (e) {
    console.log(`❌ Order failed: ${e.message?.slice(0, 60)}`);
    return null;
  }
}

async function processNewTrades(trades) {
  for (const trade of trades) {
    const tradeId = `${trade.timestamp}-${trade.asset}-${trade.side}`;

    if (seenTrades.has(tradeId)) continue;
    seenTrades.add(tradeId);

    // Only process recent trades (last 5 minutes)
    const tradeTime = trade.timestamp * 1000;
    const age = Date.now() - tradeTime;
    if (age > 5 * 60 * 1000) continue;

    const side = trade.side?.toUpperCase();
    const price = parseFloat(trade.price);
    const tokenId = trade.asset;
    const title = trade.title || trade.market || "Unknown";

    if (!side || !price || !tokenId) continue;

    console.log(`\n🔔 cigarettes ${side}: ${title?.slice(0, 40)}...`);
    console.log(`   Price: ${price}, Size: $${(trade.size * price).toFixed(2)}`);

    // Copy the trade
    await placeBet(tokenId, side, price, title);
  }
}

async function monitorLoop() {
  console.log("\n📡 Checking for new trades...");

  const trades = await fetchTargetTrades();
  if (trades.length > 0) {
    // Prime seen trades on first run
    if (seenTrades.size === 0) {
      trades.forEach(t => seenTrades.add(`${t.timestamp}-${t.asset}-${t.side}`));
      console.log(`   Primed ${trades.length} existing trades`);
    } else {
      await processNewTrades(trades);
    }
  }

  // Status
  console.log(`\n📊 Status: ${dailyTrades} trades today | Daily P&L: $${dailyPnL.toFixed(2)}`);
}

async function main() {
  console.log("=".repeat(50));
  console.log("REAL COPY TRADING BOT - cigarettes");
  console.log("=".repeat(50));
  console.log(`Bet size: $${BET_SIZE}`);
  console.log(`Target: ${TARGET_WALLET}`);
  console.log(`Max daily trades: ${MAX_DAILY_TRADES}`);
  console.log(`Max daily loss: $${MAX_DAILY_LOSS}`);
  console.log("=".repeat(50));

  await initClient();

  // Initial check
  await monitorLoop();

  // Poll loop
  console.log(`\n🔄 Polling every ${POLL_INTERVAL/1000}s...`);
  setInterval(monitorLoop, POLL_INTERVAL);
}

main().catch(e => {
  console.error("Fatal error:", e.message);
  process.exit(1);
});
