FROM node:24.16.0-bookworm-slim AS build
WORKDIR /app/apps/client
COPY apps/client/package*.json ./
RUN npm ci
COPY apps/client/tsconfig.json apps/client/vite.config.ts ./
COPY apps/client/src ./src
COPY protocol/schemas /app/protocol/schemas
RUN npm run build

FROM node:24.16.0-bookworm-slim
WORKDIR /app/apps/client
ENV NODE_ENV=production HOST=0.0.0.0 PORT=4310 CAMCTL_DATA_DIR=/app/data
COPY apps/client/package*.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/apps/client/dist ./dist
COPY --from=build /app/apps/client/src/server ./src/server
COPY --from=build /app/apps/client/src/shared ./src/shared
COPY --from=build /app/apps/client/src/domain ./src/domain
COPY --from=build /app/protocol/schemas /app/protocol/schemas
EXPOSE 4310
CMD ["npm", "start"]
