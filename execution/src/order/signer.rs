//! EIP-712 Order Signer for Polymarket CTF Exchange
//!
//! Signs orders according to the CTF Exchange typed data format.

use anyhow::{anyhow, Result};
use ethers::core::k256::ecdsa::SigningKey;
use ethers::signers::{LocalWallet, Signer};
use ethers::types::{H256, U256};
use sha3::{Digest, Keccak256};
use tracing::debug;

use super::builder::Order;

/// Signed order ready for submission
#[derive(Debug, Clone)]
pub struct SignedOrder {
    pub order: Order,
    pub signature: String,
}

/// EIP-712 domain separator
#[derive(Debug)]
struct EIP712Domain {
    name: String,
    version: String,
    chain_id: U256,
    verifying_contract: String,
}

/// Order signer using EIP-712 typed data
pub struct EIP712Signer {
    wallet: LocalWallet,
    domain_separator: H256,
    chain_id: u64,
}

impl EIP712Signer {
    pub fn new(private_key: &str, chain_id: u64, verifying_contract: &str) -> Result<Self> {
        // Parse private key (handle with/without 0x prefix)
        let key_bytes = if private_key.starts_with("0x") {
            hex::decode(&private_key[2..])?
        } else {
            hex::decode(private_key)?
        };

        let signing_key = SigningKey::from_slice(&key_bytes)
            .map_err(|e| anyhow!("Invalid private key: {}", e))?;

        let wallet = LocalWallet::from(signing_key).with_chain_id(chain_id);

        // Calculate domain separator
        let domain = EIP712Domain {
            name: "Polymarket CTF Exchange".to_string(),
            version: "1".to_string(),
            chain_id: U256::from(chain_id),
            verifying_contract: verifying_contract.to_string(),
        };

        let domain_separator = Self::hash_domain(&domain);

        debug!(
            address = %wallet.address(),
            chain_id = chain_id,
            "EIP712 signer initialized"
        );

        Ok(Self {
            wallet,
            domain_separator,
            chain_id,
        })
    }

    /// Sign an order using EIP-712
    pub fn sign_order(&self, order: &Order) -> Result<SignedOrder> {
        // Hash the order struct
        let struct_hash = self.hash_order(order);

        // Create EIP-712 digest
        let digest = self.create_typed_data_hash(struct_hash);

        // Sign the digest
        let signature = self.sign_digest(digest)?;

        Ok(SignedOrder {
            order: order.clone(),
            signature,
        })
    }

    /// Hash the EIP-712 domain
    fn hash_domain(domain: &EIP712Domain) -> H256 {
        let type_hash = Keccak256::digest(
            b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
        );

        let name_hash = Keccak256::digest(domain.name.as_bytes());
        let version_hash = Keccak256::digest(domain.version.as_bytes());

        let mut chain_id_bytes = [0u8; 32];
        domain.chain_id.to_big_endian(&mut chain_id_bytes);

        let contract_bytes = Self::parse_address(&domain.verifying_contract);

        let mut data = Vec::new();
        data.extend_from_slice(&type_hash);
        data.extend_from_slice(&name_hash);
        data.extend_from_slice(&version_hash);
        data.extend_from_slice(&chain_id_bytes);
        data.extend_from_slice(&Self::pad_address(&contract_bytes));

        H256::from_slice(&Keccak256::digest(&data))
    }

    /// Hash an order struct (Order type hash)
    fn hash_order(&self, order: &Order) -> H256 {
        // Order type hash
        let type_hash = Keccak256::digest(
            b"Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,uint8 signatureType)",
        );

        let mut data = Vec::new();
        data.extend_from_slice(&type_hash);

        // Encode each field
        data.extend_from_slice(&Self::encode_uint256(&order.salt));
        data.extend_from_slice(&Self::pad_address(&Self::parse_address(&order.maker)));
        data.extend_from_slice(&Self::pad_address(&Self::parse_address(&order.signer)));
        data.extend_from_slice(&Self::pad_address(&Self::parse_address(&order.taker)));
        data.extend_from_slice(&Self::encode_uint256(&order.token_id));
        data.extend_from_slice(&Self::encode_uint256(&order.maker_amount));
        data.extend_from_slice(&Self::encode_uint256(&order.taker_amount));
        data.extend_from_slice(&Self::encode_uint256(&order.expiration));
        data.extend_from_slice(&Self::encode_uint256(&order.nonce));
        data.extend_from_slice(&Self::encode_uint256(&order.fee_rate_bps));
        data.extend_from_slice(&Self::encode_uint8(order.side.as_u8()));
        data.extend_from_slice(&Self::encode_uint8(order.signature_type));

        H256::from_slice(&Keccak256::digest(&data))
    }

    /// Create the final EIP-712 typed data hash
    fn create_typed_data_hash(&self, struct_hash: H256) -> H256 {
        let mut data = Vec::new();
        data.push(0x19);
        data.push(0x01);
        data.extend_from_slice(self.domain_separator.as_bytes());
        data.extend_from_slice(struct_hash.as_bytes());

        H256::from_slice(&Keccak256::digest(&data))
    }

    /// Sign a digest and return hex-encoded signature
    fn sign_digest(&self, digest: H256) -> Result<String> {
        // Use blocking for sync signing
        let rt = tokio::runtime::Handle::try_current()
            .ok()
            .map(|h| h.block_on(self.wallet.sign_hash(digest.into())))
            .transpose()
            .map_err(|e| anyhow!("Signing failed: {}", e))?;

        // If we're not in an async context, create a simple signature
        let signature = match rt {
            Some(sig) => sig,
            None => {
                // Fallback: create runtime for signing
                let rt = tokio::runtime::Runtime::new()?;
                rt.block_on(self.wallet.sign_hash(digest.into()))
                    .map_err(|e| anyhow!("Signing failed: {}", e))?
            }
        };

        // Format as 0x + r + s + v
        let mut sig_bytes = Vec::with_capacity(65);
        sig_bytes.extend_from_slice(&signature.r().to_fixed_bytes());
        sig_bytes.extend_from_slice(&signature.s().to_fixed_bytes());
        sig_bytes.push(signature.v() as u8);

        Ok(format!("0x{}", hex::encode(sig_bytes)))
    }

    /// Parse address from hex string
    fn parse_address(addr: &str) -> [u8; 20] {
        let addr = addr.strip_prefix("0x").unwrap_or(addr);
        let mut bytes = [0u8; 20];
        if let Ok(decoded) = hex::decode(addr) {
            if decoded.len() >= 20 {
                bytes.copy_from_slice(&decoded[..20]);
            }
        }
        bytes
    }

    /// Pad address to 32 bytes
    fn pad_address(addr: &[u8; 20]) -> [u8; 32] {
        let mut padded = [0u8; 32];
        padded[12..].copy_from_slice(addr);
        padded
    }

    /// Encode string number as uint256
    fn encode_uint256(value: &str) -> [u8; 32] {
        let mut bytes = [0u8; 32];
        if let Ok(n) = value.parse::<u128>() {
            let n_bytes = n.to_be_bytes();
            bytes[16..].copy_from_slice(&n_bytes);
        }
        bytes
    }

    /// Encode u8 as 32 bytes
    fn encode_uint8(value: u8) -> [u8; 32] {
        let mut bytes = [0u8; 32];
        bytes[31] = value;
        bytes
    }

    /// Get signer address
    pub fn address(&self) -> String {
        format!("{:?}", self.wallet.address())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_signer_creation() {
        // Test with a known private key (DO NOT use in production)
        let test_key = "0000000000000000000000000000000000000000000000000000000000000001";
        let signer = EIP712Signer::new(
            test_key,
            137,
            "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        );
        assert!(signer.is_ok());
    }

    #[test]
    fn test_address_parsing() {
        let addr = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
        let parsed = EIP712Signer::parse_address(addr);
        assert_eq!(parsed.len(), 20);
    }
}
