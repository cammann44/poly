FROM node:20-slim

WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN npm install --production

# Copy real trading bot
COPY scripts/real_copy_trade.mjs ./

# Environment variables set in Railway
ENV NODE_ENV=production

# Run real trading bot
CMD ["node", "real_copy_trade.mjs"]
