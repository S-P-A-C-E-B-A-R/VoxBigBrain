FROM node:22-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && cp node_modules/livekit-client/dist/livekit-client.umd.js public-livekit-client.js

COPY server.mjs .
COPY public ./public
RUN mv public-livekit-client.js public/livekit-client.js

USER node
EXPOSE 8080
CMD ["node", "server.mjs"]
