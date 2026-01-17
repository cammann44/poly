//! CLOB Client for order submission to Polymarket
//!
//! Handles REST API communication with Polymarket's CLOB.

use anyhow::{anyhow, Result};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info};

use crate::order::signer::SignedOrder;

/// CLOB order submission request
#[derive(Debug, Serialize)]
struct OrderSubmission {
    order: OrderPayload,
    owner: String,
    #[serde(rename = "orderType")]
    order_type: String,
}

#[derive(Debug, Serialize)]
struct OrderPayload {
    salt: String,
    maker: String,
    signer: String,
    taker: String,
    #[serde(rename = "tokenId")]
    token_id: String,
    #[serde(rename = "makerAmount")]
    maker_amount: String,
    #[serde(rename = "takerAmount")]
    taker_amount: String,
    expiration: String,
    nonce: String,
    #[serde(rename = "feeRateBps")]
    fee_rate_bps: String,
    side: String,
    #[serde(rename = "signatureType")]
    signature_type: u8,
    signature: String,
}

/// CLOB order response
#[derive(Debug, Deserialize)]
struct OrderResponse {
    #[serde(rename = "orderID")]
    order_id: Option<String>,
    success: Option<bool>,
    error: Option<String>,
    #[serde(rename = "errorMsg")]
    error_msg: Option<String>,
}

/// CLOB REST API client
pub struct CLOBClient {
    client: Client,
    base_url: String,
    api_key: String,
    api_secret: String,
    api_passphrase: String,
}

impl CLOBClient {
    pub fn new(
        base_url: String,
        api_key: String,
        api_secret: String,
        api_passphrase: String,
    ) -> Self {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .expect("Failed to create HTTP client");

        Self {
            client,
            base_url,
            api_key,
            api_secret,
            api_passphrase,
        }
    }

    /// Submit a signed order to the CLOB
    pub async fn submit_order(&self, signed_order: SignedOrder) -> Result<String> {
        let url = format!("{}/order", self.base_url);

        let side_str = match signed_order.order.side {
            crate::order::builder::Side::Buy => "BUY",
            crate::order::builder::Side::Sell => "SELL",
        };

        let submission = OrderSubmission {
            order: OrderPayload {
                salt: signed_order.order.salt,
                maker: signed_order.order.maker.clone(),
                signer: signed_order.order.signer,
                taker: signed_order.order.taker,
                token_id: signed_order.order.token_id,
                maker_amount: signed_order.order.maker_amount,
                taker_amount: signed_order.order.taker_amount,
                expiration: signed_order.order.expiration,
                nonce: signed_order.order.nonce,
                fee_rate_bps: signed_order.order.fee_rate_bps,
                side: side_str.to_string(),
                signature_type: signed_order.order.signature_type,
                signature: signed_order.signature,
            },
            owner: signed_order.order.maker,
            order_type: "GTC".to_string(),
        };

        debug!(url = %url, "Submitting order to CLOB");

        let response = self
            .client
            .post(&url)
            .header("Content-Type", "application/json")
            .header("POLY-ADDRESS", &self.api_key)
            .header("POLY-SIGNATURE", &self.api_secret)
            .header("POLY-TIMESTAMP", &self.get_timestamp())
            .header("POLY-PASSPHRASE", &self.api_passphrase)
            .json(&submission)
            .send()
            .await
            .map_err(|e| anyhow!("HTTP request failed: {}", e))?;

        let status = response.status();

        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            error!(status = %status, body = %body, "CLOB request failed");
            return Err(anyhow!("CLOB request failed with status {}: {}", status, body));
        }

        let order_response: OrderResponse = response
            .json()
            .await
            .map_err(|e| anyhow!("Failed to parse response: {}", e))?;

        if let Some(err) = order_response.error.or(order_response.error_msg) {
            return Err(anyhow!("CLOB error: {}", err));
        }

        order_response
            .order_id
            .ok_or_else(|| anyhow!("No order ID in response"))
    }

    /// Get current timestamp for API headers
    fn get_timestamp(&self) -> String {
        use std::time::{SystemTime, UNIX_EPOCH};
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .to_string()
    }

    /// Check if the CLOB is healthy
    pub async fn health_check(&self) -> Result<bool> {
        let url = format!("{}/", self.base_url);

        match self.client.get(&url).send().await {
            Ok(response) => Ok(response.status().is_success()),
            Err(_) => Ok(false),
        }
    }

    /// Get order book for a token
    pub async fn get_orderbook(&self, token_id: &str) -> Result<serde_json::Value> {
        let url = format!("{}/book?token_id={}", self.base_url, token_id);

        let response = self
            .client
            .get(&url)
            .send()
            .await
            .map_err(|e| anyhow!("HTTP request failed: {}", e))?;

        response
            .json()
            .await
            .map_err(|e| anyhow!("Failed to parse orderbook: {}", e))
    }

    /// Cancel an order
    pub async fn cancel_order(&self, order_id: &str) -> Result<bool> {
        let url = format!("{}/order/{}", self.base_url, order_id);

        let response = self
            .client
            .delete(&url)
            .header("POLY-ADDRESS", &self.api_key)
            .header("POLY-SIGNATURE", &self.api_secret)
            .header("POLY-TIMESTAMP", &self.get_timestamp())
            .header("POLY-PASSPHRASE", &self.api_passphrase)
            .send()
            .await
            .map_err(|e| anyhow!("HTTP request failed: {}", e))?;

        Ok(response.status().is_success())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_client_creation() {
        let client = CLOBClient::new(
            "https://clob.polymarket.com".to_string(),
            "test_key".to_string(),
            "test_secret".to_string(),
            "test_passphrase".to_string(),
        );

        assert!(client.base_url.contains("clob.polymarket.com"));
    }
}
