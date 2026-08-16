FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

ARG NEXT_PUBLIC_API_URL=http://localhost:8010
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

# Fresh whenever source actually changed (this RUN sits after COPY, so
# Docker's cache invalidates it exactly when the app changes) - lets the
# sidebar answer "is this deploy actually running the code I just pushed"
# without needing git available in the build context (the context is
# ./web, not the repo root, so a real commit SHA isn't reachable here).
# Next.js loads .env.production.local automatically during `next build`
# and inlines NEXT_PUBLIC_* values into the client bundle at build time.
RUN echo "NEXT_PUBLIC_BUILD_TIME=$(date -u +'%Y-%m-%d %H:%M UTC')" >> .env.production.local

RUN npm run build

EXPOSE 3000

CMD ["npm", "run", "start"]
