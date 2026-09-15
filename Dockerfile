FROM node:24.16.0-bookworm-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY tsconfig.json vite.config.ts ./
COPY src ./src
COPY docs/superpowers/specs/camctl/schemas ./docs/superpowers/specs/camctl/schemas
RUN npm run build

FROM node:24.16.0-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production HOST=0.0.0.0 PORT=4310 CAMCTL_DATA_DIR=/app/data
COPY package*.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
COPY --from=build /app/src/server ./src/server
COPY --from=build /app/src/shared ./src/shared
COPY --from=build /app/src/domain ./src/domain
COPY --from=build /app/docs/superpowers/specs/camctl/schemas ./docs/superpowers/specs/camctl/schemas
EXPOSE 4310
CMD ["npm", "start"]
