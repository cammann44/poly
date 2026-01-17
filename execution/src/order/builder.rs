//! Order Builder for Polymarket CLOB
//!
//! Constructs order structs compliant with Polymarket's CTF Exchange.

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};

/// Order side
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Side {
    #[serde(rename = "BUY")]
    Buy,
    #[serde(rename = "SELL")]
    Sell,
}

impl Side {
    pub fn from_str(s: &str) -> Result<Self> {
        match s.to_uppercase().as_str() {
            "BUY" => Ok(Side::Buy),
            "SELL" => Ok(Side::Sell),
            _ => Err(anyhow!("Invalid side: {}", s)),
        }
    }

    pub fn as_u8(&self) -> u8 {
        match self {
            Side::Buy => 0,
            Side::Sell => 1,
        }
    }
}

/// Order type
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrderType {
    GTC, // Good Till Cancelled
    GTD, // Good Till Date
    FOK, // Fill Or Kill
    IOC, // Immediate Or Cancel
}

impl Default for OrderType {
    fn default() -> Self {
        OrderType::GTC
    }
}

/// Polymarket CLOB Order
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Order {
    /// Unique salt for order uniqueness
    pub salt: String,
    /// Maker address (our wallet)
    pub maker: String,
    /// Signer address (same as maker for EOA)
    pub signer: String,
    /// Taker address (0x0 for any)
    pub taker: String,
    /// Token ID (condition ID + outcome index)
    pub token_id: String,
    /// Maker amount in base units (shares * 10^6)
    pub maker_amount: String,
    /// Taker amount in base units (USDC * 10^6)
    pub taker_amount: String,
    /// Order expiration timestamp (0 for no expiry)
    pub expiration: String,
    /// Nonce for replay protection
    pub nonce: String,
    /// Fee rate in basis points
    pub fee_rate_bps: String,
    /// Order side
    pub side: Side,
    /// Signature type (0 for EOA, 1 for Poly Proxy, 2 for Poly Gnosis Safe)
    pub signature_type: u8,
}

/// Order builder
pub struct OrderBuilder {
    maker: String,
    chain_id: u64,
    nonce_counter: std::sync::atomic::AtomicU64,
}

impl OrderBuilder {
    pub fn new(maker: String, chain_id: u64) -> Self {
        Self {
            maker,
            chain_id,
            nonce_counter: std::sync::atomic::AtomicU64::new(0),
        }
    }

    /// Build an order from parameters
    ///
    /// # Arguments
    /// * `token_id` - The token ID to trade
    /// * `side` - "BUY" or "SELL"
    /// * `size` - Size in USD
    /// * `price` - Price between 0 and 1
    pub fn build_order(
        &self,
        token_id: &str,
        side: &str,
        size: f64,
        price: f64,
    ) -> Result<Order> {
        let side = Side::from_str(side)?;

        // Validate inputs
        if size <= 0.0 {
            return Err(anyhow!("Invalid size: {}", size));
        }
        if price <= 0.0 || price >= 1.0 {
            return Err(anyhow!("Invalid price: {}", price));
        }

        // Generate unique salt
        let salt = self.generate_salt();

        // Calculate amounts based on side
        // For BUY: maker_amount = shares, taker_amount = USDC cost
        // For SELL: maker_amount = USDC received, taker_amount = shares
        let (maker_amount, taker_amount) = match side {
            Side::Buy => {
                // Buying shares with USDC
                let shares = size / price;
                let usdc_cost = size;
                (
                    self.to_base_units(shares),
                    self.to_base_units(usdc_cost),
                )
            }
            Side::Sell => {
                // Selling shares for USDC
                let shares = size / price;
                let usdc_received = size;
                (
                    self.to_base_units(usdc_received),
                    self.to_base_units(shares),
                )
            }
        };

        // Get next nonce
        let nonce = self.nonce_counter
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);

        Ok(Order {
            salt,
            maker: self.maker.clone(),
            signer: self.maker.clone(),
            taker: "0x0000000000000000000000000000000000000000".to_string(),
            token_id: token_id.to_string(),
            maker_amount,
            taker_amount,
            expiration: "0".to_string(), // No expiry
            nonce: nonce.to_string(),
            fee_rate_bps: "0".to_string(), // Fee handled by CLOB
            side,
            signature_type: 0, // EOA signature
        })
    }

    /// Generate unique salt using timestamp and random bytes
    fn generate_salt(&self) -> String {
        use std::time::{SystemTime, UNIX_EPOCH};

        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();

        let random: u64 = rand::random();

        format!("{}{}", timestamp, random)
    }

    /// Convert to base units (6 decimals for USDC)
    fn to_base_units(&self, amount: f64) -> String {
        let base = (amount * 1_000_000.0).round() as u64;
        base.to_string()
    }
}

// Simple random number generation for salt
mod rand {
    use std::time::{SystemTime, UNIX_EPOCH};

    static mut SEED: u64 = 0;

    pub fn random() -> u64 {
        unsafe {
            if SEED == 0 {
                SEED = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos() as u64;
            }
            // Simple xorshift
            SEED ^= SEED << 13;
            SEED ^= SEED >> 7;
            SEED ^= SEED << 17;
            SEED
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_buy_order() {
        let builder = OrderBuilder::new(
            "0x1234567890123456789012345678901234567890".to_string(),
            137,
        );

        let order = builder
            .build_order("12345", "BUY", 100.0, 0.5)
            .unwrap();

        assert_eq!(order.side, Side::Buy);
        assert_eq!(order.token_id, "12345");
        assert_eq!(order.maker_amount, "200000000"); // 200 shares at $0.50
        assert_eq!(order.taker_amount, "100000000"); // $100 USDC
    }

    #[test]
    fn test_build_sell_order() {
        let builder = OrderBuilder::new(
            "0x1234567890123456789012345678901234567890".to_string(),
            137,
        );

        let order = builder
            .build_order("12345", "SELL", 100.0, 0.5)
            .unwrap();

        assert_eq!(order.side, Side::Sell);
    }

    #[test]
    fn test_invalid_price() {
        let builder = OrderBuilder::new(
            "0x1234567890123456789012345678901234567890".to_string(),
            137,
        );

        assert!(builder.build_order("12345", "BUY", 100.0, 1.5).is_err());
        assert!(builder.build_order("12345", "BUY", 100.0, 0.0).is_err());
    }
}
