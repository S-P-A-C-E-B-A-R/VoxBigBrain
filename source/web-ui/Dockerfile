FROM node:22-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && cp node_modules/livekit-client/dist/livekit-client.umd.js public-livekit-client.js

COPY server.mjs .
COPY db.mjs user-admin.mjs ./
COPY public ./public
RUN mv public-livekit-client.js public/livekit-client.js

RUN mkdir /data && chown node:node /data
USER node
EXPOSE 8080
CMD ["node", "server.mjs"]
