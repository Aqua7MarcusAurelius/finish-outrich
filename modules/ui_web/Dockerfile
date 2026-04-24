FROM node:22-alpine

WORKDIR /app

# Install deps first for better layer caching. The bind mount in
# docker-compose overlays /app at runtime, so node_modules is kept in a
# named volume to avoid being hidden by the host's empty directory.
COPY package.json ./
RUN npm install

COPY . .

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
