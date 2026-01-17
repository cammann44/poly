//! Polymarket Copy-Trading Execution Service
//!
//! High-performance Rust service for order signing and submission.
//! Receives order requests via Unix socket IPC and executes on CLOB.

mod order;
mod submission;

use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

use order::builder::OrderBuilder;
use order::signer::EIP712Signer;
use submission::clob::CLOBClient;

/// Configuration loaded from environment and config file
#[derive(Debug, Clone)]
pub struct Config {
    pub private_key: String,
    pub wallet_address: String,
    pub clob_rest_url: String,
    pub api_key: String,
    pub api_secret: String,
    pub api_passphrase: String,
    pub execution_socket: String,
    pub chain_id: u64,
    pub ctf_exchange: String,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        dotenvy::dotenv().ok();

        Ok(Self {
            private_key: std::env::var("PRIVATE_KEY")
                .unwrap_or_else(|_| "0".repeat(64)),
            wallet_address: std::env::var("WALLET_ADDRESS")
                .unwrap_or_else(|_| "0x".to_string() + &"0".repeat(40)),
            clob_rest_url: std::env::var("CLOB_REST_URL")
                .unwrap_or_else(|_| "https://clob.polymarket.com".to_string()),
            api_key: std::env::var("POLY_API_KEY").unwrap_or_default(),
            api_secret: std::env::var("POLY_API_SECRET").unwrap_or_default(),
            api_passphrase: std::env::var("POLY_API_PASSPHRASE").unwrap_or_default(),
            execution_socket: std::env::var("EXECUTION_SOCKET")
                .unwrap_or_else(|_| "/tmp/poly_execution.sock".to_string()),
            chain_id: std::env::var("CHAIN_ID")
                .unwrap_or_else(|_| "137".to_string())
                .parse()
                .unwrap_or(137),
            ctf_exchange: std::env::var("CTF_EXCHANGE")
                .unwrap_or_else(|_| "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E".to_string()),
        })
    }
}

/// Incoming order request from Decision service
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderRequest {
    #[serde(rename = "type")]
    pub msg_type: String,
    pub timestamp: f64,
    pub market_id: String,
    pub token_id: String,
    pub side: String,
    pub size: f64,
    pub price: f64,
    pub original_signal: Option<OriginalSignal>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OriginalSignal {
    pub wallet: String,
    pub size: f64,
    pub source: String,
}

/// Order execution result
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionResult {
    pub success: bool,
    pub order_id: Option<String>,
    pub error: Option<String>,
    pub latency_ms: u64,
}

/// Main execution service
struct ExecutionService {
    config: Arc<Config>,
    signer: Arc<EIP712Signer>,
    order_builder: Arc<OrderBuilder>,
    clob_client: Arc<CLOBClient>,
}

impl ExecutionService {
    fn new(config: Config) -> Result<Self> {
        let config = Arc::new(config);

        let signer = Arc::new(EIP712Signer::new(
            &config.private_key,
            config.chain_id,
            &config.ctf_exchange,
        )?);

        let order_builder = Arc::new(OrderBuilder::new(
            config.wallet_address.clone(),
            config.chain_id,
        ));

        let clob_client = Arc::new(CLOBClient::new(
            config.clob_rest_url.clone(),
            config.api_key.clone(),
            config.api_secret.clone(),
            config.api_passphrase.clone(),
        ));

        Ok(Self {
            config,
            signer,
            order_builder,
            clob_client,
        })
    }

    async fn run(&self) -> Result<()> {
        let socket_path = &self.config.execution_socket;

        // Remove existing socket file
        if Path::new(socket_path).exists() {
            std::fs::remove_file(socket_path)?;
        }

        // Create Unix socket listener
        let listener = UnixListener::bind(socket_path)?;
        info!("Execution service listening on {}", socket_path);

        // Set socket permissions
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o660))?;
        }

        loop {
            match listener.accept().await {
                Ok((stream, _addr)) => {
                    let service = self.clone_for_handler();
                    tokio::spawn(async move {
                        if let Err(e) = service.handle_connection(stream).await {
                            error!("Connection handler error: {}", e);
                        }
                    });
                }
                Err(e) => {
                    error!("Failed to accept connection: {}", e);
                }
            }
        }
    }

    fn clone_for_handler(&self) -> Self {
        Self {
            config: Arc::clone(&self.config),
            signer: Arc::clone(&self.signer),
            order_builder: Arc::clone(&self.order_builder),
            clob_client: Arc::clone(&self.clob_client),
        }
    }

    async fn handle_connection(&self, stream: tokio::net::UnixStream) -> Result<()> {
        let (reader, mut writer) = stream.into_split();
        let mut reader = BufReader::new(reader);
        let mut line = String::new();

        info!("Decision service connected");

        loop {
            line.clear();
            let bytes_read = reader.read_line(&mut line).await?;

            if bytes_read == 0 {
                info!("Decision service disconnected");
                break;
            }

            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }

            // Parse order request
            match serde_json::from_str::<OrderRequest>(trimmed) {
                Ok(request) => {
                    if request.msg_type == "ORDER_REQUEST" {
                        let result = self.execute_order(request).await;
                        let response = serde_json::to_string(&result)? + "\n";
                        writer.write_all(response.as_bytes()).await?;
                    }
                }
                Err(e) => {
                    warn!("Invalid order request: {}", e);
                }
            }
        }

        Ok(())
    }

    async fn execute_order(&self, request: OrderRequest) -> ExecutionResult {
        let start = std::time::Instant::now();

        info!(
            market = %request.market_id,
            side = %request.side,
            size = %request.size,
            "Executing order"
        );

        // Build order
        let order = match self.order_builder.build_order(
            &request.token_id,
            &request.side,
            request.size,
            request.price,
        ) {
            Ok(o) => o,
            Err(e) => {
                return ExecutionResult {
                    success: false,
                    order_id: None,
                    error: Some(format!("Failed to build order: {}", e)),
                    latency_ms: start.elapsed().as_millis() as u64,
                };
            }
        };

        // Sign order
        let signed_order = match self.signer.sign_order(&order) {
            Ok(s) => s,
            Err(e) => {
                return ExecutionResult {
                    success: false,
                    order_id: None,
                    error: Some(format!("Failed to sign order: {}", e)),
                    latency_ms: start.elapsed().as_millis() as u64,
                };
            }
        };

        // Submit to CLOB
        match self.clob_client.submit_order(signed_order).await {
            Ok(order_id) => {
                let latency = start.elapsed().as_millis() as u64;
                info!(
                    order_id = %order_id,
                    latency_ms = %latency,
                    "Order submitted successfully"
                );
                ExecutionResult {
                    success: true,
                    order_id: Some(order_id),
                    error: None,
                    latency_ms: latency,
                }
            }
            Err(e) => {
                let latency = start.elapsed().as_millis() as u64;
                error!(
                    error = %e,
                    latency_ms = %latency,
                    "Order submission failed"
                );
                ExecutionResult {
                    success: false,
                    order_id: None,
                    error: Some(e.to_string()),
                    latency_ms: latency,
                }
            }
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("poly_execution=info".parse()?),
        )
        .json()
        .init();

    info!("Starting Polymarket Execution Service");

    let config = Config::from_env()?;
    let service = ExecutionService::new(config)?;

    service.run().await
}
